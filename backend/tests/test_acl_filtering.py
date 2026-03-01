"""
Jira Cortex - ACL Filtering Tests

Tests for permission-aware vector search.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from app.services.vector_store import VectorStore, SearchResult
from qdrant_client import models

class TestACLFiltering:
    """Tests for ACL-based access control."""
    
    @pytest.fixture
    def vector_store(self, mock_settings, monkeypatch):
        monkeypatch.setattr("app.services.vector_store.get_settings", lambda: mock_settings)
        store = VectorStore()
        # Mock Qdrant client
        store._client = AsyncMock()
        return store
    
    @pytest.mark.asyncio
    async def test_search_with_no_access_returns_empty(self, vector_store):
        """Search with no project access should return empty."""
        results = await vector_store.search(
            query_embedding=[0.1] * 1536,
            tenant_id="tenant-1",
            project_access=[],  # No access
            limit=3
        )
        
        assert results == []
    
    @pytest.mark.asyncio
    async def test_search_builds_correct_filter(self, vector_store):
        """Search should build proper ACL filter."""
        # Mock search to capture the filter
        mock_search = AsyncMock(return_value=[])
        vector_store._client.search = mock_search
        
        await vector_store.search(
            query_embedding=[0.1] * 1536,
            tenant_id="tenant-1",
            project_access=["PROJ-1", "PROJ-2"],
            limit=3
        )
        
        # Verify search was called
        assert mock_search.called
        
        # Check filter structure — vector_store.search passes query_filter as kwarg
        call_kwargs = mock_search.call_args.kwargs
        query_filter = call_kwargs.get("query_filter")
        
        assert query_filter is not None
        # Should have must conditions: tenant_id + project_id
        assert hasattr(query_filter, "must")
        assert len(query_filter.must) == 2
    
    @pytest.mark.asyncio
    async def test_results_include_all_metadata(self, vector_store):
        """Search results should include all required metadata."""
        # Mock search response
        mock_result = MagicMock()
        mock_result.id = "point-1"
        mock_result.score = 0.85
        mock_result.payload = {
            "content": "Issue content",
            "issue_key": "PROJ-123",
            "issue_title": "Test Issue",
            "project_id": "10001",
            "issue_url": "https://jira.atlassian.net/browse/PROJ-123",
            "metadata": {}
        }
        
        vector_store._client.search = AsyncMock(return_value=[mock_result])
        
        results = await vector_store.search(
            query_embedding=[0.1] * 1536,
            tenant_id="tenant-1",
            project_access=["10001"],
            limit=3
        )
        
        assert len(results) == 1
        assert results[0].issue_key == "PROJ-123"
        assert results[0].score == 0.85
        assert results[0].content == "Issue content"
        assert results[0].url == "https://jira.atlassian.net/browse/PROJ-123"


class TestTenantIsolation:
    """Tests for tenant isolation in storage."""
    
    @pytest.fixture
    def vector_store(self, mock_settings, monkeypatch):
        monkeypatch.setattr("app.services.vector_store.get_settings", lambda: mock_settings)
        store = VectorStore()
        store._client = AsyncMock()
        return store
    
    @pytest.mark.asyncio
    async def test_upsert_includes_tenant_id(self, vector_store):
        """Upsert should tag documents with tenant ID."""
        from app.utils.text_processing import TextChunk
        
        chunk = TextChunk(
            content="Test content",
            chunk_index=0,
            total_chunks=1,
            token_count=10,
            content_hash="abc123"
        )
        
        mock_upsert = AsyncMock(return_value=None)
        vector_store._client.upsert = mock_upsert
        
        await vector_store.upsert_chunks(
            chunks=[chunk],
            embeddings=[[0.1] * 1536],
            issue_key="PROJ-123",
            issue_title="Test",
            project_id="10001",
            tenant_id="tenant-1",
            issue_url="https://example.com/PROJ-123"
        )
        
        assert mock_upsert.called
        
        # Points are passed as kwarg
        call_kwargs = mock_upsert.call_args.kwargs
        points = call_kwargs.get("points")
        
        assert points is not None
        assert len(points) == 1
        assert points[0].payload["tenant_id"] == "tenant-1"
        assert points[0].payload["project_id"] == "10001"
        assert points[0].payload["issue_key"] == "PROJ-123"
    
    @pytest.mark.asyncio
    async def test_delete_requires_tenant_match(self, vector_store):
        """Delete should filter by tenant ID."""
        mock_delete = AsyncMock(return_value=None)
        vector_store._client.delete = mock_delete
        
        await vector_store.delete_issue(
            tenant_id="tenant-1",
            issue_key="PROJ-123"
        )
        
        assert mock_delete.called
        
        # Verify filter includes both tenant_id and issue_key
        call_kwargs = mock_delete.call_args.kwargs
        points_selector = call_kwargs.get("points_selector")
        assert points_selector is not None
        must_conditions = points_selector.filter.must
        keys = [c.key for c in must_conditions]
        assert "tenant_id" in keys
        assert "issue_key" in keys


class TestCacheKeyACL:
    """Tests for ACL-aware cache keys."""
    
    @pytest.fixture
    def cache_service(self, mock_settings, monkeypatch):
        monkeypatch.setattr("app.services.cache.get_settings", lambda: mock_settings)
        from app.services.cache import CacheService
        return CacheService()
    
    def test_cache_key_includes_tenant(self, cache_service):
        """Cache key should include tenant ID."""
        key1 = cache_service._generate_cache_key(
            query="test query",
            tenant_id="tenant-1",
            project_access=["PROJ-1"]
        )
        
        key2 = cache_service._generate_cache_key(
            query="test query",
            tenant_id="tenant-2",  # Different tenant
            project_access=["PROJ-1"]
        )
        
        assert key1 != key2  # Different tenants = different keys
    
    def test_cache_key_includes_project_access(self, cache_service):
        """Cache key should include project access list."""
        key1 = cache_service._generate_cache_key(
            query="test query",
            tenant_id="tenant-1",
            project_access=["PROJ-1"]
        )
        
        key2 = cache_service._generate_cache_key(
            query="test query",
            tenant_id="tenant-1",
            project_access=["PROJ-1", "PROJ-2"]  # Different access
        )
        
        assert key1 != key2  # Different access = different keys
    
    def test_cache_key_is_deterministic(self, cache_service):
        """Same inputs should produce same cache key."""
        key1 = cache_service._generate_cache_key(
            query="test query",
            tenant_id="tenant-1",
            project_access=["PROJ-2", "PROJ-1"]  # Order shouldn't matter
        )
        
        key2 = cache_service._generate_cache_key(
            query="test query",
            tenant_id="tenant-1",
            project_access=["PROJ-1", "PROJ-2"]  # Same projects, different order
        )
        
        assert key1 == key2  # Should be same (sorted)
