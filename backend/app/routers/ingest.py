"""
Jira Cortex - Ingestion Router

Async batch ingestion and real-time webhook updates.
Solves Trap 1 (Forge timeout) and Trap 2 (stale data).
"""

from fastapi import APIRouter, Depends, HTTPException, BackgroundTasks, Request
from slowapi import Limiter
from slowapi.util import get_remote_address
import structlog
from pydantic import BaseModel

from app.config import get_settings
from app.models.schemas import (
    IngestBatchRequest,
    IngestSingleRequest,
    IngestConfluenceBatchRequest,
    IngestResponse,
    ErrorResponse,
    UserContext
)

from app.services.background_processor import (
    get_background_processor,
    BackgroundProcessor
)
from app.services.vector_store import get_vector_store
from app.auth.dependencies import get_current_user
from app.services.cache import get_cache_service, CacheService
from app.services.billing import get_billing_service, BillingService

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["ingest"])

# Rate limiter
limiter = Limiter(key_func=get_remote_address)




@router.post(
    "/ingest/batch",
    response_model=IngestResponse,
    status_code=202,  # Accepted - processing async
    responses={
        401: {"model": ErrorResponse},
        400: {"model": ErrorResponse},
        429: {"model": ErrorResponse}
    },
    summary="Ingest a batch of issues (async)",
    description="""
    Submit a batch of Jira issues for ingestion into the knowledge base.
    
    **IMPORTANT**: This endpoint returns immediately with 202 Accepted.
    The actual processing happens in the background to avoid timeout.
    Use the returned job_id to check status via /ingest/status/{job_id}.
    """
)
@limiter.limit("10/minute")  # FIXED: Rate limit batch ingestion
async def ingest_batch(
    request: Request,  # Required for rate limiter
    ingest_request: IngestBatchRequest,
    background_tasks: BackgroundTasks,
    user: UserContext = Depends(get_current_user),
    processor: BackgroundProcessor = Depends(get_background_processor),
    billing_service: BillingService = Depends(get_billing_service),
    cache_service: CacheService = Depends(get_cache_service)
) -> IngestResponse:
    """
    Accept batch of issues for async processing.
    
    This solves Trap 1: Forge 25-second timeout
    - Returns 202 immediately
    - Processes in background
    - Client can poll for status
    """
    settings = get_settings()
    
    # CRITICAL: Check tenant subscription (prevents "free lunch" abuse)
    if not await billing_service.is_tenant_allowed(user.tenant_id):
        logger.warning("tenant_not_allowed_batch", tenant_id=user.tenant_id)
        raise HTTPException(
            status_code=403,
            detail={
                "error": "SUBSCRIPTION_REQUIRED",
                "message": "Your organization is not subscribed to Jira Cortex."
            }
        )
    
    # CRITICAL: Check wallet has sufficient funds for batch
    # Estimate: ~$0.001 per issue for embeddings
    estimated_batch_cost = len(ingest_request.issues) * 0.001
    if not await billing_service.has_sufficient_funds(user.tenant_id, estimated_batch_cost):
        logger.warning(
            "insufficient_funds_batch",
            tenant_id=user.tenant_id,
            issue_count=len(ingest_request.issues),
            estimated_cost=estimated_batch_cost
        )
        raise HTTPException(
            status_code=402,  # Payment Required
            detail={
                "error": "INSUFFICIENT_CREDITS",
                "message": f"Insufficient credits for batch ingestion. Estimated cost: ${estimated_batch_cost:.2f}"
            }
        )
    
    # Validate batch size
    if len(ingest_request.issues) > settings.ingestion_batch_size:
        raise HTTPException(
            status_code=400,
            detail={
                "error": "BATCH_TOO_LARGE",
                "message": f"Maximum batch size is {settings.ingestion_batch_size}"
            }
        )
    
    # Verify tenant matches
    if ingest_request.tenant_id != user.tenant_id:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "TENANT_MISMATCH",
                "message": "Cannot ingest for different tenant"
            }
        )
    
    # Create background job
    job = await processor.create_job(
        tenant_id=ingest_request.tenant_id,
        issue_count=len(ingest_request.issues)
    )
    
    # Build base URL from tenant
    base_url = f"https://{user.tenant_id}.atlassian.net"
    
    # Schedule background processing (billing is now handled inside processor)
    background_tasks.add_task(
        processor.process_batch_async,
        job_id=job.job_id,
        issues=ingest_request.issues,
        tenant_id=ingest_request.tenant_id,
        base_url=base_url
    )
    
    logger.info(
        "batch_ingestion_scheduled",
        job_id=job.job_id,
        issue_count=len(ingest_request.issues)
    )
    
    # Invalidate cache
    await cache_service.invalidate_tenant(user.tenant_id)
    
    return IngestResponse(
        job_id=job.job_id,
        status="accepted",
        message=f"Processing {len(ingest_request.issues)} issues in background",
        estimated_completion_seconds=processor.estimate_completion_time(len(ingest_request.issues))
    )

@router.post(
    "/ingest/confluence/batch",
    response_model=IngestResponse,
    status_code=202,
    summary="Ingest a batch of Confluence pages (async)"
)
@limiter.limit("10/minute")
async def ingest_confluence_batch(
    request: Request,
    ingest_request: IngestConfluenceBatchRequest,
    background_tasks: BackgroundTasks,
    user: UserContext = Depends(get_current_user),
    processor: BackgroundProcessor = Depends(get_background_processor),
    billing_service: BillingService = Depends(get_billing_service),
    cache_service: CacheService = Depends(get_cache_service)
) -> IngestResponse:
    settings = get_settings()
    
    if not await billing_service.is_tenant_allowed(user.tenant_id):
        raise HTTPException(status_code=403, detail={"error": "SUBSCRIPTION_REQUIRED", "message": "Not subscribed."})
        
    estimated_batch_cost = len(ingest_request.pages) * 0.001
    if not await billing_service.has_sufficient_funds(user.tenant_id, estimated_batch_cost):
        raise HTTPException(status_code=402, detail={"error": "INSUFFICIENT_CREDITS", "message": "Insufficient credits"})
        
    if len(ingest_request.pages) > settings.ingestion_batch_size:
        raise HTTPException(status_code=400, detail={"error": "BATCH_TOO_LARGE", "message": f"Max {settings.ingestion_batch_size}"})
        
    if ingest_request.tenant_id != user.tenant_id:
        raise HTTPException(status_code=403, detail={"error": "TENANT_MISMATCH", "message": "Tenant mismatch"})
        
    job = await processor.create_job(tenant_id=ingest_request.tenant_id, issue_count=len(ingest_request.pages))
    
    background_tasks.add_task(
        processor.process_confluence_batch_async,
        job_id=job.job_id,
        pages=ingest_request.pages,
        tenant_id=ingest_request.tenant_id
    )
    
    # Invalidate cache
    await cache_service.invalidate_tenant(user.tenant_id)
    
    return IngestResponse(
        job_id=job.job_id,
        status="accepted",
        message=f"Processing {len(ingest_request.pages)} pages in background",
        estimated_completion_seconds=processor.estimate_completion_time(len(ingest_request.pages))
    )

@router.post(
    "/ingest/single",
    response_model=IngestResponse,
    responses={
        401: {"model": ErrorResponse},
        429: {"model": ErrorResponse},
        500: {"model": ErrorResponse}
    },
    summary="Ingest a single issue (real-time)",
    description="""
    Process a single issue update from webhook trigger.
    
    This solves Trap 2: Stale data
    - Called by Forge webhook (avi:jira:updated:issue)
    - Processes immediately for real-time sync
    - Supports create, update, and delete events
    """
)
@limiter.limit("120/minute")  # FIXED: Rate limit webhook updates (higher limit)
async def ingest_single(
    request: Request,  # Required for rate limiter
    ingest_request: IngestSingleRequest,
    user: UserContext = Depends(get_current_user),
    processor: BackgroundProcessor = Depends(get_background_processor),
    cache_service: CacheService = Depends(get_cache_service),
    billing_service: BillingService = Depends(get_billing_service)  # FIXED: Injected
) -> IngestResponse:
    """
    Process a single issue update from webhook.
    
    This provides real-time sync when issues are updated in Jira.
    """
    # CRITICAL: Check tenant subscription (prevents "free lunch" abuse)
    if not await billing_service.is_tenant_allowed(user.tenant_id):
        logger.warning("tenant_not_allowed_single", tenant_id=user.tenant_id)
        raise HTTPException(
            status_code=403,
            detail={
                "error": "SUBSCRIPTION_REQUIRED",
                "message": "Your organization is not subscribed to Jira Cortex."
            }
        )
    
    # CRITICAL: Check wallet has funds ($0.001 per single issue)
    if not await billing_service.has_sufficient_funds(user.tenant_id, 0.001):
        logger.warning("insufficient_funds_single", tenant_id=user.tenant_id)
        raise HTTPException(
            status_code=402,
            detail={
                "error": "INSUFFICIENT_CREDITS",
                "message": "Insufficient credits for issue ingestion."
            }
        )
    
    # Verify tenant matches
    if ingest_request.tenant_id != user.tenant_id:
        raise HTTPException(
            status_code=403,
            detail={
                "error": "TENANT_MISMATCH",
                "message": "Cannot ingest for different tenant"
            }
        )
    
    base_url = f"https://{user.tenant_id}.atlassian.net"
    
    try:
        # Process synchronously (single issue is fast enough)
        success, tokens_used = await processor.process_single_issue_sync(
            issue=ingest_request.issue,
            tenant_id=ingest_request.tenant_id,
            base_url=base_url,
            event_type=ingest_request.event_type
        )
        
        if success:
            # FIXED: Record billing for single issue ingestion
            if tokens_used > 0:
                await billing_service.record_usage(
                    tenant_id=user.tenant_id,
                    user_account_id=user.account_id,
                    operation="ingest",
                    input_tokens=tokens_used,
                    output_tokens=0,
                    model="text-embedding-3-small",
                    cached=False
                )
            
            # Invalidate cache for this issue and tenant
            await cache_service.invalidate_issue(
                tenant_id=ingest_request.tenant_id,
                issue_key=ingest_request.issue.key
            )
            await cache_service.invalidate_tenant(ingest_request.tenant_id)
            
            logger.info(
                "single_issue_ingested",
                issue_key=ingest_request.issue.key,
                event_type=ingest_request.event_type,
                tokens_billed=tokens_used
            )
            
            return IngestResponse(
                job_id=f"single-{ingest_request.issue.key}",
                status="completed",
                message=f"Issue {ingest_request.issue.key} processed ({ingest_request.event_type})"
            )
        else:
            raise HTTPException(
                status_code=500,
                detail={
                    "error": "PROCESSING_FAILED",
                    "message": f"Failed to process issue {ingest_request.issue.key}"
                }
            )
            
    except Exception as e:
        logger.error(
            "single_ingestion_failed",
            issue_key=ingest_request.issue.key,
            error=str(e)
        )
        raise HTTPException(
            status_code=500,
            detail={
                "error": "INTERNAL_ERROR",
                "message": "Failed to process issue"
            }
        )


class TenantProvisionRequest(BaseModel):
    tenant_id: str

@router.delete(
    "/tenant/provision",
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse}
    },
    summary="Handle app uninstallation (GDPR data wipe)"
)
@limiter.limit("5/minute")
async def delete_tenant_data(
    request: Request,
    provision_req: TenantProvisionRequest,
    user: UserContext = Depends(get_current_user)
) -> dict:
    # Verify tenant matches JWT context
    if provision_req.tenant_id != user.tenant_id:
        raise HTTPException(
            status_code=403, 
            detail={"error": "TENANT_MISMATCH", "message": "Tenant mismatch"}
        )
        
    try:
        vector_store = get_vector_store()
        await vector_store.delete_tenant(provision_req.tenant_id)
        
        logger.info("tenant_data_wiped", tenant_id=provision_req.tenant_id)
        return {"status": "success", "message": "Tenant data wiped"}
    except Exception as e:
        logger.error("tenant_wipe_failed", error=str(e))
        raise HTTPException(
            status_code=500, 
            detail={"error": "INTERNAL_ERROR", "message": "Failed to wipe tenant data"}
        )


@router.post(
    "/tenant/provision",
    responses={
        401: {"model": ErrorResponse},
        403: {"model": ErrorResponse}
    },
    summary="Handle app installation (Provision tenant)"
)
@limiter.limit("5/minute")
async def provision_tenant_data(
    request: Request,
    provision_req: TenantProvisionRequest,
    user: UserContext = Depends(get_current_user)
) -> dict:
    if provision_req.tenant_id != user.tenant_id:
        raise HTTPException(
            status_code=403, 
            detail={"error": "TENANT_MISMATCH", "message": "Tenant mismatch"}
        )
        
    try:
        vector_store = get_vector_store()
        await vector_store.initialize_collection()
        
        logger.info("tenant_data_provisioned", tenant_id=provision_req.tenant_id)
        return {"status": "success", "message": "Tenant successfully provisioned"}
    except Exception as e:
        logger.error("tenant_provision_failed", error=str(e))
        raise HTTPException(
            status_code=500, 
            detail={"error": "INTERNAL_ERROR", "message": "Failed to provision tenant data"}
        )


@router.get(
    "/ingest/status/{job_id}",
    responses={
        404: {"model": ErrorResponse}
    },
    summary="Get ingestion job status"
)
@limiter.limit("120/minute")  # FIXED: Rate limit status checks
async def get_job_status(
    request: Request,  # Required for rate limiter
    job_id: str,
    user: UserContext = Depends(get_current_user),
    processor: BackgroundProcessor = Depends(get_background_processor)
):
    """
    Get the status of a background ingestion job.
    """
    job = await processor.get_job(job_id)
    
    if not job:
        raise HTTPException(
            status_code=404,
            detail={"error": "NOT_FOUND", "message": "Job not found"}
        )
    
    # Verify tenant
    if job.tenant_id != user.tenant_id:
        raise HTTPException(
            status_code=404,
            detail={"error": "NOT_FOUND", "message": "Job not found"}
        )
    
    return {
        "job_id": job.job_id,
        "status": job.status.value,
        "progress": {
            "total": job.total_issues,
            "processed": job.processed_issues,
            "failed": job.failed_issues,
            "percent": round(job.progress_percent, 1)
        },
        "created_at": job.created_at.isoformat(),
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
        "error": job.error_message
    }
