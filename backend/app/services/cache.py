"""
Jira Cortex - Cache Service

Redis-based semantic caching with TTL.
"""

import json
import hashlib
from typing import Optional
import structlog
import redis.asyncio as redis

from app.config import get_settings
from app.models.schemas import QueryResponse

logger = structlog.get_logger(__name__)


class CacheError(Exception):
    """Raised when cache operations fail."""
    pass


class CacheService:
    """
    Redis cache for query deduplication.
    
    Features:
    - Semantic query caching (10s TTL)
    - Cache key includes user's project access for security
    - Graceful degradation on Redis failure
    """
    
    # Key prefixes for namespacing
    PREFIX_QUERY = "cortex:query:"
    PREFIX_EMBEDDING = "cortex:embed:"
    
    def __init__(self):
        self.settings = get_settings()
        self._client: Optional[redis.Redis] = None
    
    async def get_client(self) -> redis.Redis:
        """Get or create Redis client."""
        if self._client is None:
            self._client = redis.from_url(
                self.settings.redis_url,
                password=self.settings.redis_password or None,
                decode_responses=True,
                socket_timeout=5.0,
                socket_connect_timeout=5.0
            )
        return self._client
    
    def _generate_cache_key(
        self, 
        query: str, 
        tenant_id: str, 
        project_access: list
    ) -> str:
        """
        Generate a unique cache key that includes ACL context.
        
        SECURITY: Keys include project access to prevent cache poisoning
        """
        # Sort project access for deterministic hashing
        sorted_projects = sorted(project_access)
        
        key_content = json.dumps({
            "q": query.lower().strip(),
            "t": tenant_id,
            "p": sorted_projects
        }, sort_keys=True)
        
        # Hash for fixed-length key
        key_hash = hashlib.sha256(key_content.encode()).hexdigest()[:32]
        
        return f"{self.PREFIX_QUERY}{key_hash}"
    
    async def get_cached_response(
        self,
        query: str,
        tenant_id: str,
        project_access: list
    ) -> Optional[QueryResponse]:
        """
        Get cached query response if exists.
        
        Args:
            query: User's query
            tenant_id: Tenant ID
            project_access: User's project access list
            
        Returns:
            Cached QueryResponse or None
        """
        try:
            client = await self.get_client()
            key = self._generate_cache_key(query, tenant_id, project_access)
            
            cached = await client.get(key)
            
            if cached:
                logger.info("cache_hit", key=key[:20])
                data = json.loads(cached)
                
                # Mark as cached
                response = QueryResponse(**data)
                response.cached = True
                return response
            
            logger.debug("cache_miss", key=key[:20])
            return None
            
        except redis.RedisError as e:
            logger.warning("cache_get_failed", error=str(e))
            return None
        except json.JSONDecodeError as e:
            logger.warning("cache_decode_failed", error=str(e))
            return None
    
    async def cache_response(
        self,
        query: str,
        tenant_id: str,
        project_access: list,
        response: QueryResponse
    ) -> bool:
        """
        Cache a query response.
        
        Args:
            query: User's query
            tenant_id: Tenant ID
            project_access: User's project access list
            response: Response to cache
            
        Returns:
            True if cached successfully
        """
        try:
            client = await self.get_client()
            key = self._generate_cache_key(query, tenant_id, project_access)
            
            # Serialize response (exclude cached flag)
            data = response.model_dump(exclude={"cached"})
            
            await client.setex(
                key,
                self.settings.redis_cache_ttl_seconds,
                json.dumps(data)
            )
            
            logger.info("cache_set", key=key[:20], ttl=self.settings.redis_cache_ttl_seconds)
            return True
            
        except redis.RedisError as e:
            logger.warning("cache_set_failed", error=str(e))
            return False
    
    async def invalidate_issue(self, tenant_id: str, issue_key: str) -> int:
        """
        Invalidate cache entries related to an issue.
        
        Called when an issue is updated to ensure fresh data.
        
        Note: This is a best-effort operation. We can't easily invalidate
        all queries that might include this issue, so we rely on TTL.
        
        Args:
            tenant_id: Tenant ID
            issue_key: Issue key that was updated
            
        Returns:
            Number of keys invalidated (0 if not implemented)
        """
        # With short TTL (10s), explicit invalidation isn't critical
        # The cache will naturally refresh quickly
        logger.info("cache_invalidation_triggered", 
                   tenant_id=tenant_id, 
                   issue_key=issue_key)
        return 0
    
    async def cache_embedding(
        self,
        text: str,
        embedding: list,
        ttl_seconds: int = 3600
    ) -> bool:
        """
        Cache a text embedding to reduce OpenAI API calls.
        
        Args:
            text: Original text
            embedding: Embedding vector
            ttl_seconds: Cache TTL (default 1 hour)
            
        Returns:
            True if cached successfully
        """
        try:
            client = await self.get_client()
            
            # Hash the text for the key
            text_hash = hashlib.sha256(text.encode()).hexdigest()[:32]
            key = f"{self.PREFIX_EMBEDDING}{text_hash}"
            
            await client.setex(
                key,
                ttl_seconds,
                json.dumps(embedding)
            )
            
            return True
            
        except redis.RedisError as e:
            logger.warning("embedding_cache_failed", error=str(e))
            return False
    
    async def get_cached_embedding(self, text: str) -> Optional[list]:
        """
        Get cached embedding for text.
        
        Args:
            text: Text to look up
            
        Returns:
            Cached embedding or None
        """
        try:
            client = await self.get_client()
            
            text_hash = hashlib.sha256(text.encode()).hexdigest()[:32]
            key = f"{self.PREFIX_EMBEDDING}{text_hash}"
            
            cached = await client.get(key)
            
            if cached:
                return json.loads(cached)
            
            return None
            
        except (redis.RedisError, json.JSONDecodeError) as e:
            logger.warning("embedding_cache_get_failed", error=str(e))
            return None
    
    async def health_check(self) -> bool:
        """Check if Redis is healthy."""
        try:
            client = await self.get_client()
            await client.ping()
            return True
        except Exception:
            return False
    
    async def close(self) -> None:
        """Close Redis connection."""
        if self._client:
            await self._client.close()
            self._client = None


# Singleton instance
_cache_service: Optional[CacheService] = None


def get_cache_service() -> CacheService:
    """Get or create cache service singleton."""
    global _cache_service
    if _cache_service is None:
        _cache_service = CacheService()
    return _cache_service
