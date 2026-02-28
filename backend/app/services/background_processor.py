"""
Jira Cortex - Background Task Processor

Async ingestion to avoid Forge timeout (25s limit).
Includes billing integration for token tracking.
"""

import asyncio
import uuid
from typing import Dict, List, Optional, Tuple
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
import structlog

def utc_now():
    return datetime.now(timezone.utc)

from app.config import get_settings
from app.models.schemas import JiraIssue, ConfluencePage
from app.services.vector_store import get_vector_store
from app.services.llm import get_llm_service
from app.services.billing import get_billing_service
from app.utils.text_processing import get_text_processor

logger = structlog.get_logger(__name__)


class JobStatus(str, Enum):
    """Background job status."""
    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


@dataclass
class IngestionJob:
    """Tracks an ingestion job."""
    job_id: str
    tenant_id: str
    status: JobStatus = JobStatus.PENDING
    total_issues: int = 0
    processed_issues: int = 0
    failed_issues: int = 0
    total_tokens_used: int = 0  # FIXED: Track tokens for billing
    created_at: datetime = field(default_factory=utc_now)
    completed_at: Optional[datetime] = None
    error_message: Optional[str] = None
    
    @property
    def progress_percent(self) -> float:
        if self.total_issues == 0:
            return 0.0
        return (self.processed_issues / self.total_issues) * 100


class BackgroundProcessor:
    """
    Background task processor for async ingestion.
    
    Solves Trap 1: Forge 25-second timeout
    - Accepts data immediately
    - Processes in background at own pace
    - Reports progress via job status
    - FIXED: Tracks and bills token usage
    - FIXED: Persists jobs to Redis for crash recovery
    """
    
    # Max concurrent embedding requests
    MAX_CONCURRENT_EMBEDDINGS = 5
    
    # Max issues to process per batch within a job
    BATCH_SIZE = 10
    
    # Redis key prefix for jobs
    REDIS_JOB_PREFIX = "cortex:job:"
    
    # Job TTL in Redis (24 hours)
    JOB_TTL_SECONDS = 86400
    
    def __init__(self):
        self.settings = get_settings()
        self._jobs: Dict[str, IngestionJob] = {}
        self._processing_lock = asyncio.Lock()
        self._redis_client = None
    
    async def _get_redis(self):
        """Get or create Redis client."""
        if self._redis_client is None:
            try:
                import redis.asyncio as redis
                self._redis_client = redis.from_url(
                    self.settings.redis_url,
                    decode_responses=True
                )
            except Exception as e:
                logger.warning("redis_connection_failed", error=str(e))
                return None
        return self._redis_client
    
    async def _persist_job(self, job: IngestionJob) -> None:
        """Persist job state to Redis."""
        redis = await self._get_redis()
        if redis:
            try:
                import json
                job_data = {
                    "job_id": job.job_id,
                    "tenant_id": job.tenant_id,
                    "status": job.status.value,
                    "total_issues": job.total_issues,
                    "processed_issues": job.processed_issues,
                    "failed_issues": job.failed_issues,
                    "total_tokens_used": job.total_tokens_used,
                    "created_at": job.created_at.isoformat(),
                    "completed_at": job.completed_at.isoformat() if job.completed_at else None,
                    "error_message": job.error_message
                }
                key = f"{self.REDIS_JOB_PREFIX}{job.job_id}"
                await redis.setex(key, self.JOB_TTL_SECONDS, json.dumps(job_data))
            except Exception as e:
                logger.warning("job_persist_failed", job_id=job.job_id, error=str(e))
    
    async def _load_job_from_redis(self, job_id: str) -> Optional[IngestionJob]:
        """Load job state from Redis."""
        redis = await self._get_redis()
        if redis:
            try:
                import json
                key = f"{self.REDIS_JOB_PREFIX}{job_id}"
                data = await redis.get(key)
                if data:
                    job_data = json.loads(data)
                    return IngestionJob(
                        job_id=job_data["job_id"],
                        tenant_id=job_data["tenant_id"],
                        status=JobStatus(job_data["status"]),
                        total_issues=job_data["total_issues"],
                        processed_issues=job_data["processed_issues"],
                        failed_issues=job_data["failed_issues"],
                        total_tokens_used=job_data["total_tokens_used"],
                        created_at=datetime.fromisoformat(job_data["created_at"]),
                        completed_at=datetime.fromisoformat(job_data["completed_at"]) if job_data["completed_at"] else None,
                        error_message=job_data["error_message"]
                    )
            except Exception as e:
                logger.warning("job_load_failed", job_id=job_id, error=str(e))
        return None
    
    async def recover_orphaned_jobs(self) -> int:
        """
        Recover orphaned jobs on startup.
        
        Marks any jobs in PROCESSING state as FAILED since the server
        restarted and they won't be resumed.
        
        Returns number of jobs recovered.
        """
        redis = await self._get_redis()
        if not redis:
            return 0
        
        recovered = 0
        try:
            import json
            # Scan for all job keys
            async for key in redis.scan_iter(f"{self.REDIS_JOB_PREFIX}*"):
                data = await redis.get(key)
                if data:
                    job_data = json.loads(data)
                    if job_data["status"] == JobStatus.PROCESSING.value:
                        # Mark as failed - server restarted mid-processing
                        job_data["status"] = JobStatus.FAILED.value
                        job_data["error_message"] = "Server restarted during processing"
                        job_data["completed_at"] = utc_now().isoformat()
                        await redis.setex(key, self.JOB_TTL_SECONDS, json.dumps(job_data))
                        logger.warning(
                            "orphaned_job_recovered",
                            job_id=job_data["job_id"],
                            tenant_id=job_data["tenant_id"]
                        )
                        recovered += 1
        except Exception as e:
            logger.error("orphan_recovery_failed", error=str(e))
        
        if recovered > 0:
            logger.info("orphaned_jobs_recovered", count=recovered)
        
        return recovered
    
    async def create_job(self, tenant_id: str, issue_count: int) -> IngestionJob:
        """
        Create a new ingestion job.
        
        Args:
            tenant_id: Tenant identifier
            issue_count: Number of issues to process
            
        Returns:
            New IngestionJob instance
        """
        job = IngestionJob(
            job_id=str(uuid.uuid4()),
            tenant_id=tenant_id,
            total_issues=issue_count
        )
        
        self._jobs[job.job_id] = job
        
        # Persist to Redis for crash recovery
        await self._persist_job(job)
        
        logger.info(
            "ingestion_job_created",
            job_id=job.job_id,
            tenant_id=tenant_id,
            issue_count=issue_count
        )
        
        return job
    
    async def get_job(self, job_id: str) -> Optional[IngestionJob]:
        """Get job status by ID (from memory or Redis)."""
        # Try in-memory first
        if job_id in self._jobs:
            return self._jobs[job_id]
        
        # Fallback to Redis
        job = await self._load_job_from_redis(job_id)
        if job:
            self._jobs[job_id] = job  # Cache in memory
        return job
    
    def estimate_completion_time(self, issue_count: int) -> int:
        """
        Estimate completion time in seconds.
        
        Based on:
        - ~2s per issue for embedding
        - Batch processing reduces this
        """
        # Rough estimate: 200ms per issue with batching
        return max(5, issue_count // 5)
    
    async def process_batch_async(
        self,
        job_id: str,
        issues: List[JiraIssue],
        tenant_id: str,
        base_url: str = "https://jira.atlassian.com"
    ) -> None:
        """
        Process a batch of issues asynchronously.
        
        This runs in the background after returning 202 to client.
        FIXED: Now tracks and bills token usage.
        
        Args:
            job_id: Job ID for tracking
            issues: List of issues to process
            tenant_id: Tenant identifier
            base_url: Jira base URL for links
        """
        job = self._jobs.get(job_id)
        if not job:
            logger.error("job_not_found", job_id=job_id)
            return
        
        job.status = JobStatus.PROCESSING
        
        text_processor = get_text_processor()
        llm_service = get_llm_service()
        vector_store = get_vector_store()
        billing_service = get_billing_service()
        
        total_tokens_used = 0
        
        try:
            # Process in smaller batches
            for batch_start in range(0, len(issues), self.BATCH_SIZE):
                batch = issues[batch_start:batch_start + self.BATCH_SIZE]
                
                # Process batch with limited concurrency
                semaphore = asyncio.Semaphore(self.MAX_CONCURRENT_EMBEDDINGS)
                
                async def process_issue(issue: JiraIssue) -> Tuple[bool, int]:
                    async with semaphore:
                        try:
                            return await self._process_single_issue_with_usage(
                                issue=issue,
                                tenant_id=tenant_id,
                                base_url=base_url,
                                text_processor=text_processor,
                                llm_service=llm_service,
                                vector_store=vector_store
                            )
                        except Exception as e:
                            logger.error(
                                "issue_processing_failed",
                                issue_key=issue.key,
                                error=str(e)
                            )
                            return False, 0
                
                # Process batch concurrently
                results = await asyncio.gather(
                    *[process_issue(issue) for issue in batch],
                    return_exceptions=True
                )
                
                # Update job progress
                for result in results:
                    if isinstance(result, tuple):
                        success, tokens = result
                        if success:
                            job.processed_issues += 1
                            total_tokens_used += tokens
                        else:
                            job.failed_issues += 1
                    else:
                        job.failed_issues += 1
                
                logger.info(
                    "batch_processed",
                    job_id=job_id,
                    progress=f"{job.processed_issues}/{job.total_issues}",
                    tokens_so_far=total_tokens_used
                )
                
                # FIXED: Incremental billing after each batch (prevents lost revenue on crash)
                batch_tokens = sum(t for _, t in results if isinstance(_, tuple) and _)
                if batch_tokens > 0:
                    # Record for analytics
                    await billing_service.record_usage(
                        tenant_id=tenant_id,
                        user_account_id="system",  # Background job
                        operation="ingest_batch",
                        input_tokens=batch_tokens,
                        output_tokens=0,
                        model=self.settings.openai_embedding_model,
                        cached=False
                    )
                    
                    # CRITICAL: Deduct from wallet (Financial Fortress)
                    batch_cost = billing_service.calculate_embedding_cost(batch_tokens)
                    await billing_service.deduct_balance(
                        tenant_id=tenant_id,
                        cost=batch_cost,
                        description=f"Batch ingestion: {len(batch)} issues"
                    )
                
                # Persist job state after each batch for crash recovery
                await self._persist_job(job)
            
            # Mark job complete
            job.status = JobStatus.COMPLETED
            job.completed_at = utc_now()
            job.total_tokens_used = total_tokens_used
            
            logger.info(
                "ingestion_job_completed",
                job_id=job_id,
                processed=job.processed_issues,
                failed=job.failed_issues,
                total_tokens=total_tokens_used
            )
            
        except Exception as e:
            job.status = JobStatus.FAILED
            job.error_message = str(e)
            job.completed_at = utc_now()
            
            logger.error(
                "ingestion_job_failed",
                job_id=job_id,
                error=str(e)
            )
    
    async def _process_single_issue_with_usage(
        self,
        issue: JiraIssue,
        tenant_id: str,
        base_url: str,
        text_processor,
        llm_service,
        vector_store
    ) -> Tuple[bool, int]:
        """
        Process a single issue: clean, chunk, embed, store.
        
        Returns:
            Tuple of (success, tokens_used)
        """
        # Format issue text
        formatted_text = text_processor.format_issue_for_embedding(
            key=issue.key,
            summary=issue.summary,
            description=issue.description,
            status=issue.status.value,
            labels=issue.labels,
            comments=issue.comments
        )
        
        # Process text (clean, detect secrets, chunk)
        chunks, secrets_masked = text_processor.process(
            text=formatted_text,
            doc_id=issue.key,
            is_html=False  # Already cleaned
        )
        
        if not chunks:
            logger.warning("no_chunks_generated", issue_key=issue.key)
            return True, 0  # Not an error, just nothing to embed
        
        # Generate embeddings with usage tracking
        chunk_texts = [c.content for c in chunks]
        embeddings, tokens_used = await llm_service.generate_embeddings_batch_with_usage(chunk_texts)
        
        # Build issue URL
        issue_url = f"{base_url}/browse/{issue.key}"
        
        # Store in vector DB with ACL
        await vector_store.upsert_chunks(
            chunks=chunks,
            embeddings=embeddings,
            issue_key=issue.key,
            issue_title=issue.summary,
            project_id=issue.project_id,
            tenant_id=tenant_id,
            issue_url=issue_url,
            additional_metadata={
                "status": issue.status.value,
                "labels": issue.labels,
                "created": issue.created.isoformat(),
                "updated": issue.updated.isoformat()
            }
        )
        
        return True, tokens_used

    async def _process_single_confluence_page_with_usage(
        self,
        page: ConfluencePage,
        tenant_id: str,
        text_processor,
        llm_service,
        vector_store
    ) -> Tuple[bool, int]:
        # Format text
        formatted_text = text_processor.format_confluence_page_for_embedding(
            page_id=page.page_id,
            title=page.title,
            body=page.body,
            space_key=page.space_key,
            labels=page.labels
        )
        
        # Process text
        chunks, secrets_masked = text_processor.process(
            text=formatted_text,
            doc_id=page.page_id,
            is_html=False
        )
        
        if not chunks:
            return True, 0
            
        chunk_texts = [c.content for c in chunks]
        embeddings, tokens_used = await llm_service.generate_embeddings_batch_with_usage(chunk_texts)
        
        await vector_store.upsert_chunks(
            chunks=chunks,
            embeddings=embeddings,
            issue_key=f"{page.space_key}-{page.page_id}",
            issue_title=page.title,
            project_id=page.space_key,
            tenant_id=tenant_id,
            issue_url=page.url,
            additional_metadata={
                "page_id": page.page_id,
                "space_key": page.space_key,
                "labels": page.labels,
                "created": page.created.isoformat(),
                "updated": page.updated.isoformat(),
                "document_type": "confluence_page"
            }
        )
        return True, tokens_used
        
    async def process_confluence_batch_async(
        self,
        job_id: str,
        pages: List[ConfluencePage],
        tenant_id: str
    ) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
            
        job.status = JobStatus.PROCESSING
        
        text_processor = get_text_processor()
        llm_service = get_llm_service()
        vector_store = get_vector_store()
        billing_service = get_billing_service()
        
        total_tokens_used = 0
        
        try:
            for batch_start in range(0, len(pages), self.BATCH_SIZE):
                batch = pages[batch_start:batch_start + self.BATCH_SIZE]
                semaphore = asyncio.Semaphore(self.MAX_CONCURRENT_EMBEDDINGS)
                
                async def process_page(page: ConfluencePage) -> Tuple[bool, int]:
                    async with semaphore:
                        try:
                            return await self._process_single_confluence_page_with_usage(
                                page=page,
                                tenant_id=tenant_id,
                                text_processor=text_processor,
                                llm_service=llm_service,
                                vector_store=vector_store
                            )
                        except Exception as e:
                            logger.error("confluence_processing_failed", page_id=page.page_id, error=str(e))
                            return False, 0
                
                results = await asyncio.gather(
                    *[process_page(p) for p in batch],
                    return_exceptions=True
                )
                
                for result in results:
                    if isinstance(result, tuple):
                        success, tokens = result
                        if success:
                            job.processed_issues += 1
                            total_tokens_used += tokens
                        else:
                            job.failed_issues += 1
                    else:
                        job.failed_issues += 1
                
                batch_tokens = sum(t for _, t in results if isinstance(_, tuple) and _)
                if batch_tokens > 0:
                    await billing_service.record_usage(
                        tenant_id=tenant_id,
                        user_account_id="system",
                        operation="ingest_batch",
                        input_tokens=batch_tokens,
                        output_tokens=0,
                        model=self.settings.openai_embedding_model,
                        cached=False
                    )
                    batch_cost = billing_service.calculate_embedding_cost(batch_tokens)
                    await billing_service.deduct_balance(
                        tenant_id=tenant_id,
                        cost=batch_cost,
                        description=f"Confluence batch ingestion: {len(batch)} pages"
                    )
                
                await self._persist_job(job)
            
            job.status = JobStatus.COMPLETED
            job.completed_at = utc_now()
            job.total_tokens_used = total_tokens_used
            
        except Exception as e:
            job.status = JobStatus.FAILED
            job.error_message = str(e)
            job.completed_at = utc_now()
    
    async def process_single_issue_sync(
        self,
        issue: JiraIssue,
        tenant_id: str,
        base_url: str = "https://jira.atlassian.com",
        event_type: str = "updated"
    ) -> Tuple[bool, int]:
        """
        Process a single issue synchronously (for webhook updates).
        
        This is for real-time updates where we want to process immediately.
        FIXED: Returns token usage for billing.
        
        Args:
            issue: Issue to process
            tenant_id: Tenant identifier
            base_url: Jira base URL
            event_type: Type of event (created, updated, deleted)
            
        Returns:
            Tuple of (success, tokens_used)
        """
        text_processor = get_text_processor()
        llm_service = get_llm_service()
        vector_store = get_vector_store()
        
        try:
            if event_type == "deleted":
                # Delete all chunks for this issue
                await vector_store.delete_issue(tenant_id, issue.key)
                logger.info("issue_deleted_from_index", issue_key=issue.key)
                return True, 0
            
            # For create/update, process normally with usage tracking
            return await self._process_single_issue_with_usage(
                issue=issue,
                tenant_id=tenant_id,
                base_url=base_url,
                text_processor=text_processor,
                llm_service=llm_service,
                vector_store=vector_store
            )
            
        except Exception as e:
            logger.error(
                "single_issue_processing_failed",
                issue_key=issue.key,
                event_type=event_type,
                error=str(e)
            )
            return False, 0
    
    def cleanup_old_jobs(self, max_age_seconds: int = 3600) -> int:
        """
        Clean up completed jobs older than max_age.
        
        Returns:
            Number of jobs cleaned up
        """
        now = utc_now()
        to_remove = []
        
        for job_id, job in self._jobs.items():
            if job.completed_at:
                age = (now - job.completed_at).total_seconds()
                if age > max_age_seconds:
                    to_remove.append(job_id)
        
        for job_id in to_remove:
            del self._jobs[job_id]
        
        if to_remove:
            logger.info("jobs_cleaned_up", count=len(to_remove))
        
        return len(to_remove)


from functools import lru_cache

@lru_cache(maxsize=1)
def get_background_processor() -> BackgroundProcessor:
    """Get or create background processor singleton."""
    return BackgroundProcessor()
