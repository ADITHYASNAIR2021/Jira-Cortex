"""
Jira Cortex - Test Configuration

Pytest configuration and shared fixtures.
"""

import pytest
import asyncio
from typing import Generator
from unittest.mock import AsyncMock, MagicMock


# pytest-asyncio >= 0.23: use asyncio_mode = "auto" in pyproject.toml or pytest.ini
# The old event_loop session-scoped fixture is deprecated.
# We use the function-scoped default instead (fastest and safest).

@pytest.fixture
def mock_settings():
    """
    Mock settings for testing.
    Provides a real Settings object with safe test values.
    Does NOT call external services.
    """
    from app.config import Settings

    return Settings(
        openai_api_key="sk-test-key-for-testing-only-yes",
        qdrant_url="http://localhost:6333",
        atlassian_client_id="test-client-id",
        atlassian_client_secret="test-client-secret-12345",
        jwt_secret_key="test-jwt-secret-key-for-testing-32chars",
        redis_url="redis://localhost:6379/1",
        app_env="development",
        allowed_tenants="",
    )


@pytest.fixture
def mock_vector_store():
    """Mock vector store with sensible defaults."""
    from app.services.vector_store import SearchResult

    mock = AsyncMock()
    mock.search.return_value = [
        SearchResult(
            id="result-1",
            content="Similar login issue resolved by updating certificate",
            score=0.85,
            issue_key="PROJ-100",
            issue_title="SSL certificate causing login failure",
            project_id="10001",
            url="https://jira.atlassian.net/browse/PROJ-100",
            metadata={}
        )
    ]
    mock.search_similar.return_value = []
    mock.upsert_chunks.return_value = None
    mock.delete_issue.return_value = 1
    mock.health_check.return_value = True
    mock.initialize_collection.return_value = None
    return mock


@pytest.fixture
def mock_llm_service():
    """Mock LLM service with sensible defaults."""
    from app.services.llm import LLMResponse

    mock = AsyncMock()
    mock.generate_embedding.return_value = [0.1] * 1536
    mock.generate_embedding_with_usage.return_value = ([0.1] * 1536, 10)
    mock.generate_embeddings_batch.return_value = [[0.1] * 1536]
    mock.generate_embeddings_batch_with_usage.return_value = ([[0.1] * 1536], 10)
    mock.generate_answer.return_value = LLMResponse(
        answer="This appears similar to PROJ-100 where an SSL certificate issue caused login failures.",
        confidence_score=85.0,
        input_tokens=500,
        output_tokens=100,
        total_tokens=600,
        model="gpt-4o"
    )
    mock.health_check.return_value = True
    return mock


@pytest.fixture
def mock_billing_service():
    """Mock billing service that always allows everything."""
    mock = AsyncMock()
    mock.is_tenant_allowed.return_value = True
    mock.has_sufficient_funds.return_value = True
    mock.deduct_balance.return_value = True
    mock.record_usage.return_value = None
    mock.calculate_embedding_cost.return_value = 0.001
    mock.calculate_query_cost.return_value = 0.002
    mock.get_balance.return_value = 100.0
    return mock


@pytest.fixture
def mock_cache_service():
    """Mock cache service — no cache hits by default."""
    mock = AsyncMock()
    mock.get_cached_response.return_value = None
    mock.cache_response.return_value = True
    mock.get_cached_embedding.return_value = None
    mock.cache_embedding.return_value = True
    mock.invalidate_issue.return_value = 0
    mock.invalidate_tenant.return_value = 0
    mock.is_event_processed.return_value = False
    mock.mark_event_processed.return_value = True
    mock.add_to_conversation.return_value = True
    mock.get_conversation.return_value = []
    mock.health_check.return_value = True
    mock.get_client.return_value = AsyncMock()
    return mock


@pytest.fixture
def mock_processor(mock_vector_store, mock_llm_service, mock_billing_service, mock_cache_service):
    """
    A BackgroundProcessor with all services injected as mocks.
    Use this instead of bare BackgroundProcessor() in tests.
    """
    from app.services.background_processor import BackgroundProcessor

    return BackgroundProcessor(
        vector_store=mock_vector_store,
        llm_service=mock_llm_service,
        billing_service=mock_billing_service,
        cache_service=mock_cache_service,
    )


@pytest.fixture
def sample_user_context():
    """Sample authenticated user context."""
    from app.models.schemas import UserContext

    return UserContext(
        account_id="test-user-123",
        email="test@example.com",
        display_name="Test User",
        tenant_id="test-tenant",
        project_access=["PROJ-1", "PROJ-2"],
        roles=["developer"]
    )


@pytest.fixture
def sample_jira_issue():
    """Sample Jira issue for ingestion tests."""
    from datetime import datetime, timezone
    from app.models.schemas import JiraIssue, IssueStatus

    now = datetime.now(timezone.utc)
    return JiraIssue(
        key="PROJ-123",
        summary="Login fails on iOS devices",
        description="<p>Users report login failure on iOS 17</p>",
        status=IssueStatus.OPEN,
        project_id="10001",
        project_key="PROJ",
        reporter_account_id="reporter-123",
        assignee_account_id="assignee-456",
        created=now,
        updated=now,
        labels=["bug", "ios"],
        components=["mobile-app"],
        comments=["Investigating the issue", "Found the root cause"]
    )
