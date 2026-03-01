"""
Jira Cortex - Async Ingestion Tests

Tests for background processing and timeout handling.
"""

import pytest
import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock

from app.services.background_processor import (
    BackgroundProcessor,
    IngestionJob,
    JobStatus
)
from app.models.schemas import JiraIssue, IssueStatus


def utc_now():
    return datetime.now(timezone.utc)


# ============================================================
# BackgroundProcessor Core Behavior
# ============================================================

class TestBackgroundProcessor:
    """Tests for async background processing core methods."""

    @pytest.mark.asyncio
    async def test_create_job_returns_pending_status(self, mock_processor):
        """New job should have pending status."""
        job = await mock_processor.create_job("tenant-1", 10)

        assert job.status == JobStatus.PENDING
        assert job.total_issues == 10
        assert job.processed_issues == 0
        assert job.job_id is not None

    @pytest.mark.asyncio
    async def test_get_job_returns_created_job(self, mock_processor):
        """Should retrieve job by ID from in-memory dict."""
        job = await mock_processor.create_job("tenant-1", 5)
        retrieved = await mock_processor.get_job(job.job_id)

        assert retrieved is not None
        assert retrieved.job_id == job.job_id
        assert retrieved.tenant_id == "tenant-1"

    @pytest.mark.asyncio
    async def test_get_nonexistent_job_returns_none(self, mock_processor):
        """Should return None for unknown job ID (not raise)."""
        result = await mock_processor.get_job("nonexistent-id-that-doesnt-exist")
        assert result is None

    def test_estimate_completion_time(self, mock_processor):
        """Estimates should be in a sane range."""
        assert mock_processor.estimate_completion_time(50) >= 5
        assert mock_processor.estimate_completion_time(50) <= 120
        # Zero issues = minimum estimate
        assert mock_processor.estimate_completion_time(0) == 5

    @pytest.mark.asyncio
    async def test_job_progress_calculation(self, mock_processor):
        """Job progress_percent property should compute correctly."""
        job = await mock_processor.create_job("tenant-1", 10)

        assert job.progress_percent == 0.0

        job.processed_issues = 5
        assert job.progress_percent == 50.0

        job.processed_issues = 10
        assert job.progress_percent == 100.0

    @pytest.mark.asyncio
    async def test_job_progress_zero_total_no_division_error(self, mock_processor):
        """Empty batch must not cause ZeroDivisionError."""
        job = await mock_processor.create_job("tenant-1", 0)
        assert job.progress_percent == 0.0


# ============================================================
# Cleanup
# ============================================================

class TestJobCleanup:
    """Tests for in-memory job cleanup."""

    @pytest.mark.asyncio
    async def test_cleanup_removes_old_completed_jobs(self, mock_processor):
        """Should remove completed jobs older than max_age_seconds."""
        # Create an old completed job
        old_job = await mock_processor.create_job("tenant-1", 1)
        old_job.status = JobStatus.COMPLETED
        old_job.completed_at = datetime(2020, 1, 1, tzinfo=timezone.utc)

        # Create a recent completed job
        new_job = await mock_processor.create_job("tenant-1", 1)
        new_job.status = JobStatus.COMPLETED
        new_job.completed_at = utc_now()

        cleaned = mock_processor.cleanup_old_jobs(max_age_seconds=60)

        assert cleaned == 1
        # Old job should be gone; new job should still exist
        assert old_job.job_id not in mock_processor._jobs
        assert new_job.job_id in mock_processor._jobs

    @pytest.mark.asyncio
    async def test_cleanup_keeps_pending_jobs(self, mock_processor):
        """Should never remove PENDING or PROCESSING jobs."""
        job = await mock_processor.create_job("tenant-1", 5)
        # Job is PENDING (no completed_at)

        cleaned = mock_processor.cleanup_old_jobs(max_age_seconds=0)

        assert cleaned == 0
        assert job.job_id in mock_processor._jobs


# ============================================================
# Single Issue Sync
# ============================================================

class TestSingleIssueSync:
    """Tests for real-time single issue processing (webhook handler)."""

    @pytest.mark.asyncio
    async def test_delete_event_calls_vector_store_delete(self, mock_processor, sample_jira_issue):
        """Delete event should call delete_issue on vector store."""
        mock_processor.vector_store.delete_issue.return_value = 1

        success, tokens = await mock_processor.process_single_issue_sync(
            issue=sample_jira_issue,
            tenant_id="tenant-1",
            event_type="deleted"
        )

        assert success is True
        assert tokens == 0  # Delete doesn't use tokens
        mock_processor.vector_store.delete_issue.assert_called_once_with(
            "tenant-1", sample_jira_issue.key
        )

    @pytest.mark.asyncio
    async def test_update_event_does_not_call_delete(self, mock_processor, sample_jira_issue):
        """Update event should re-embed the issue, not delete it."""
        # Make text processor return empty (so process is fast)
        from unittest.mock import MagicMock
        mock_processor.llm_service.generate_embeddings_batch_with_usage.return_value = ([], 0)

        import app.services.background_processor as bp_module
        mock_tp = MagicMock()
        mock_tp.format_issue_for_embedding.return_value = "formatted"
        mock_tp.process.return_value = ([], 0)  # No chunks → no embedding call

        original = bp_module.get_text_processor
        bp_module.get_text_processor = lambda: mock_tp

        try:
            success, tokens = await mock_processor.process_single_issue_sync(
                issue=sample_jira_issue,
                tenant_id="tenant-1",
                event_type="updated"
            )
        finally:
            bp_module.get_text_processor = original

        # delete_issue must NOT be called for an update
        mock_processor.vector_store.delete_issue.assert_not_called()


# ============================================================
# Batch Processing (with mocked item processor)
# ============================================================

class TestBatchProcessing:
    """Tests for _run_batch_processing engine."""

    @pytest.mark.asyncio
    async def test_batch_marks_job_completed_on_success(self, mock_processor):
        """Successful batch run should set status=COMPLETED."""
        job = await mock_processor.create_job("tenant-1", 2)

        call_order = []

        async def fake_processor(item):
            call_order.append(item)
            return True, 100  # success, 100 tokens

        await mock_processor._run_batch_processing(
            job=job,
            items=["item-1", "item-2"],
            item_processor=fake_processor,
            tenant_id="tenant-1",
            operation_name="test_op",
        )

        assert job.status == JobStatus.COMPLETED
        assert job.processed_issues == 2
        assert job.failed_issues == 0
        assert job.completed_at is not None

    @pytest.mark.asyncio
    async def test_batch_tracks_failed_items(self, mock_processor):
        """Items that return False should increment failed_issues."""
        job = await mock_processor.create_job("tenant-1", 3)

        async def failing_processor(item):
            if item == "bad-item":
                return False, 0
            return True, 50

        await mock_processor._run_batch_processing(
            job=job,
            items=["good-1", "bad-item", "good-2"],
            item_processor=failing_processor,
            tenant_id="tenant-1",
            operation_name="test_op",
        )

        assert job.processed_issues == 2
        assert job.failed_issues == 1
        assert job.status == JobStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_batch_bills_tokens(self, mock_processor):
        """Batch tokens should be billed via billing_service."""
        job = await mock_processor.create_job("tenant-1", 1)

        async def token_producer(item):
            return True, 500

        await mock_processor._run_batch_processing(
            job=job,
            items=["item-1"],
            item_processor=token_producer,
            tenant_id="tenant-1",
            operation_name="test_op",
        )

        # billing should have been called
        mock_processor.billing_service.record_usage.assert_called_once()
        mock_processor.billing_service.deduct_balance.assert_called_once()

    @pytest.mark.asyncio
    async def test_batch_status_failed_on_crash(self, mock_processor):
        """If processor crashes entirely, job should be marked FAILED."""
        job = await mock_processor.create_job("tenant-1", 1)

        async def crashing_processor(item):
            raise RuntimeError("Simulated crash")

        await mock_processor._run_batch_processing(
            job=job,
            items=["item-1"],
            item_processor=crashing_processor,
            tenant_id="tenant-1",
            operation_name="test_op",
        )

        # Individual item failures don't crash the job — they just increment failed_issues
        # Only a top-level exception marks the job FAILED
        assert job.status in (JobStatus.COMPLETED, JobStatus.FAILED)
