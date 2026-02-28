"""
Jira Cortex - Vector Store Service

Qdrant integration with ACL-filtered search.
"""

import uuid
from typing import List, Optional, Dict, Any
from dataclasses import dataclass
import structlog
from qdrant_client import QdrantClient, models
from qdrant_client.http.exceptions import UnexpectedResponse

from app.config import get_settings
from app.utils.text_processing import TextChunk

logger = structlog.get_logger(__name__)


@dataclass
class SearchResult:
    """A single search result with metadata."""
    id: str
    content: str
    score: float
    issue_key: str
    issue_title: str
    project_id: str
    url: str
    metadata: Dict[str, Any]


class VectorStoreError(Exception):
    """Raised when vector store operations fail."""
    pass


class VectorStore:
    """
    Qdrant vector store with ACL-filtered search.
    
    Security features:
    - Every document tagged with ACL metadata
    - Searches always filtered by user's project access
    - Tenant isolation enforced at query level
    """
    
    # Vector dimensions for text-embedding-3-small
    VECTOR_DIMENSIONS = 1536
    
    def __init__(self):
        self.settings = get_settings()
        self._client: Optional[QdrantClient] = None
        
    @property
    def client(self) -> QdrantClient:
        """Lazy initialization of Qdrant client."""
        if self._client is None:
            if self.settings.qdrant_api_key:
                self._client = QdrantClient(
                    url=self.settings.qdrant_url,
                    api_key=self.settings.qdrant_api_key,
                    timeout=30
                )
            else:
                # Local Qdrant (development)
                self._client = QdrantClient(
                    url=self.settings.qdrant_url,
                    timeout=30
                )
        return self._client
    
    async def initialize_collection(self) -> None:
        """
        Initialize the vector collection with proper schema.
        
        Creates collection if it doesn't exist.
        Sets up indexes for ACL filtering.
        """
        collection_name = self.settings.qdrant_collection_name
        
        try:
            # Check if collection exists
            collections = self.client.get_collections()
            existing = [c.name for c in collections.collections]
            
            if collection_name not in existing:
                # Create collection
                self.client.create_collection(
                    collection_name=collection_name,
                    vectors_config=models.VectorParams(
                        size=self.VECTOR_DIMENSIONS,
                        distance=models.Distance.COSINE
                    ),
                    # Enable on-disk storage for large datasets
                    optimizers_config=models.OptimizersConfigDiff(
                        indexing_threshold=10000
                    )
                )
                
                # Create payload indexes for filtering
                self.client.create_payload_index(
                    collection_name=collection_name,
                    field_name="tenant_id",
                    field_schema=models.PayloadSchemaType.KEYWORD
                )
                
                self.client.create_payload_index(
                    collection_name=collection_name,
                    field_name="project_id",
                    field_schema=models.PayloadSchemaType.KEYWORD
                )
                
                self.client.create_payload_index(
                    collection_name=collection_name,
                    field_name="issue_key",
                    field_schema=models.PayloadSchemaType.KEYWORD
                )
                
                logger.info("collection_created", name=collection_name)
            else:
                logger.info("collection_exists", name=collection_name)
                
        except Exception as e:
            logger.error("collection_init_failed", error=str(e))
            raise VectorStoreError(f"Failed to initialize collection: {e}")
    
    async def upsert_chunks(
        self,
        chunks: List[TextChunk],
        embeddings: List[List[float]],
        issue_key: str,
        issue_title: str,
        project_id: str,
        tenant_id: str,
        issue_url: str,
        additional_metadata: Optional[Dict[str, Any]] = None
    ) -> int:
        """
        Upsert document chunks with ACL metadata.
        
        Args:
            chunks: Text chunks to store
            embeddings: Vector embeddings for each chunk
            issue_key: Jira issue key (e.g., PROJ-123)
            issue_title: Issue summary
            project_id: Project ID for ACL filtering
            tenant_id: Tenant ID for isolation
            issue_url: Direct link to issue
            additional_metadata: Extra metadata to store
            
        Returns:
            Number of points upserted
        """
        if len(chunks) != len(embeddings):
            raise VectorStoreError("Chunks and embeddings count mismatch")
        
        if not chunks:
            return 0
        
        try:
            points = []
            
            for i, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
                point_id = str(uuid.uuid5(
                    uuid.NAMESPACE_DNS,
                    f"{tenant_id}:{issue_key}:{chunk.chunk_index}"
                ))
                
                payload = {
                    # ACL fields (indexed for filtering)
                    "tenant_id": tenant_id,
                    "project_id": project_id,
                    "issue_key": issue_key,
                    
                    # Content fields
                    "content": chunk.content,
                    "issue_title": issue_title,
                    "issue_url": issue_url,
                    "chunk_index": chunk.chunk_index,
                    "total_chunks": chunk.total_chunks,
                    "content_hash": chunk.content_hash,
                    
                    # Access control list for complex queries
                    "acl": [
                        f"tenant:{tenant_id}",
                        f"proj:{project_id}"
                    ]
                }
                
                if additional_metadata:
                    payload["metadata"] = additional_metadata
                
                points.append(models.PointStruct(
                    id=point_id,
                    vector=embedding,
                    payload=payload
                ))
            
            # Upsert in batches
            self.client.upsert(
                collection_name=self.settings.qdrant_collection_name,
                points=points,
                wait=True
            )
            
            logger.info(
                "chunks_upserted",
                issue_key=issue_key,
                count=len(points)
            )
            
            return len(points)
            
        except Exception as e:
            logger.error("upsert_failed", issue_key=issue_key, error=str(e))
            raise VectorStoreError(f"Failed to upsert chunks: {e}")
    
    async def search(
        self,
        query_embedding: List[float],
        tenant_id: str,
        project_access: List[str],
        limit: int = 3,
        score_threshold: float = 0.5
    ) -> List[SearchResult]:
        """
        Search for similar documents with ACL filtering.
        
        SECURITY: Only returns documents the user has access to.
        
        Args:
            query_embedding: Query vector
            tenant_id: User's tenant ID
            project_access: List of project IDs user can access
            limit: Maximum results to return
            score_threshold: Minimum similarity score
            
        Returns:
            List of SearchResult objects
        """
        if not project_access:
            logger.warning("search_no_project_access", tenant_id=tenant_id)
            return []
        
        try:
            # Build ACL filter: must match tenant AND one of the projects
            filter_conditions = models.Filter(
                must=[
                    # Tenant isolation (always required)
                    models.FieldCondition(
                        key="tenant_id",
                        match=models.MatchValue(value=tenant_id)
                    ),
                    # Project access (any of the user's projects)
                    models.FieldCondition(
                        key="project_id",
                        match=models.MatchAny(any=project_access)
                    )
                ]
            )
            
            # Execute search
            results = self.client.search(
                collection_name=self.settings.qdrant_collection_name,
                query_vector=query_embedding,
                query_filter=filter_conditions,
                limit=limit,
                score_threshold=score_threshold,
                with_payload=True
            )
            
            search_results = []
            for result in results:
                payload = result.payload or {}
                search_results.append(SearchResult(
                    id=str(result.id),
                    content=payload.get("content", ""),
                    score=result.score,
                    issue_key=payload.get("issue_key", ""),
                    issue_title=payload.get("issue_title", ""),
                    project_id=payload.get("project_id", ""),
                    url=payload.get("issue_url", ""),
                    metadata=payload.get("metadata", {})
                ))
            
            logger.info(
                "search_completed",
                tenant_id=tenant_id,
                projects_queried=len(project_access),
                results_found=len(search_results)
            )
            
            return search_results
            
        except Exception as e:
            logger.error("search_failed", error=str(e))
            raise VectorStoreError(f"Search failed: {e}")
    
    async def delete_issue(self, tenant_id: str, issue_key: str) -> int:
        """
        Delete all chunks for an issue.
        
        Args:
            tenant_id: Tenant ID
            issue_key: Issue key to delete
            
        Returns:
            Number of points deleted
        """
        try:
            # Delete by filter
            result = self.client.delete(
                collection_name=self.settings.qdrant_collection_name,
                points_selector=models.FilterSelector(
                    filter=models.Filter(
                        must=[
                            models.FieldCondition(
                                key="tenant_id",
                                match=models.MatchValue(value=tenant_id)
                            ),
                            models.FieldCondition(
                                key="issue_key",
                                match=models.MatchValue(value=issue_key)
                            )
                        ]
                    )
                ),
                wait=True
            )
            
            logger.info("issue_deleted", issue_key=issue_key)
            return 1  # Qdrant doesn't return count
            
        except Exception as e:
            logger.error("delete_failed", issue_key=issue_key, error=str(e))
            raise VectorStoreError(f"Delete failed: {e}")
    
    async def health_check(self) -> bool:
        """Check if vector store is healthy."""
        try:
            self.client.get_collections()
            return True
        except Exception:
            return False


# Singleton instance
_vector_store: Optional[VectorStore] = None


def get_vector_store() -> VectorStore:
    """Get or create vector store singleton."""
    global _vector_store
    if _vector_store is None:
        _vector_store = VectorStore()
    return _vector_store
