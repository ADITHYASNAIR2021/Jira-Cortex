"""
Jira Cortex - Pydantic Schemas

Type-safe request/response models with validation.
"""

from datetime import datetime, timezone
from enum import Enum
from typing import List, Optional, Dict, Any
from pydantic import BaseModel, Field, field_validator, field_validator
import re

def utc_now():
    return datetime.now(timezone.utc)


class IssueStatus(str, Enum):
    """Jira issue status types."""
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    CLOSED = "closed"


# ===========================================
# Query Schemas
# ===========================================

class QueryRequest(BaseModel):
    """
    Incoming query request from Forge app.
    """
    query: str = Field(
        ..., 
        min_length=3, 
        max_length=2000,
        description="User's natural language query"
    )
    context: Optional[Dict[str, Any]] = Field(
        default=None,
        description="Additional context from current issue"
    )
    session_id: Optional[str] = Field(
        default=None,
        description="Session ID for Conversational Memory"
    )
    
    @field_validator("query")
    @classmethod
    def sanitize_query(cls, v: str) -> str:
        """Remove potential injection attempts."""
        # Strip excessive whitespace
        v = " ".join(v.split())
        # Remove null bytes and other control characters
        v = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]', '', v)
        return v


class Citation(BaseModel):
    """Source citation for an answer."""
    issue_key: str = Field(..., description="Jira issue key (e.g., PROJ-123)")
    title: str = Field(..., description="Issue title/summary")
    url: str = Field(..., description="Direct link to the issue")
    relevance_score: float = Field(..., ge=0.0, le=1.0)


class QueryResponse(BaseModel):
    """
    Response to a query with citations and confidence.
    """
    answer: str = Field(..., description="Generated answer")
    confidence_score: float = Field(
        ..., 
        ge=0.0, 
        le=100.0,
        description="Confidence percentage (0-100)"
    )
    citations: List[Citation] = Field(
        default_factory=list,
        description="Source citations"
    )
    cached: bool = Field(default=False, description="Whether response was cached")
    processing_time_ms: int = Field(..., description="Total processing time")
    tokens_used: Optional[int] = Field(default=None, description="Total tokens consumed")


class ErrorResponse(BaseModel):
    """Standardized error response."""
    error: str = Field(..., description="Error type")
    message: str = Field(..., description="Human-readable error message")
    detail: Optional[str] = Field(default=None, description="Additional details")
    request_id: Optional[str] = Field(default=None, description="Request tracking ID")


# ===========================================
# Ingestion Schemas
# ===========================================

class JiraIssue(BaseModel):
    """Jira issue for ingestion."""
    key: str = Field(..., pattern=r'^[A-Z]+-\d+$', description="Issue key")
    summary: str = Field(..., max_length=500)
    description: Optional[str] = Field(default=None, max_length=50000)
    status: IssueStatus = Field(...)
    project_id: str = Field(..., description="Project identifier for ACL")
    project_key: str = Field(..., description="Project key")
    reporter_account_id: Optional[str] = Field(default=None)
    assignee_account_id: Optional[str] = Field(default=None)
    created: datetime
    updated: datetime
    resolved: Optional[datetime] = Field(default=None)
    labels: List[str] = Field(default_factory=list)
    components: List[str] = Field(default_factory=list)
    comments: List[str] = Field(default_factory=list, max_length=100)
    
    @field_validator("comments")
    @classmethod
    def validate_comment_sizes(cls, v):
        return [c[:10000] for c in v]  # Cap each comment at 10k chars


class IngestBatchRequest(BaseModel):
    """Batch ingestion request (async processing)."""
    issues: List[JiraIssue] = Field(
        ..., 
        min_length=1, 
        max_length=50,
        description="Batch of issues to ingest"
    )
    tenant_id: str = Field(..., description="Tenant identifier for ACL")
    force_update: bool = Field(
        default=False, 
        description="Force re-embedding even if unchanged"
    )


class IngestSingleRequest(BaseModel):
    """Single issue ingestion (webhook trigger)."""
    issue: JiraIssue
    tenant_id: str
    event_type: str = Field(
        ..., 
        pattern=r'^(created|updated|deleted)$',
        description="Type of change event"
    )

class ConfluencePage(BaseModel):
    """Confluence page for ingestion."""
    page_id: str = Field(..., description="Confluence Page ID")
    title: str = Field(..., max_length=500)
    body: Optional[str] = Field(default=None, max_length=500000)
    space_key: str = Field(..., description="Space key (mapped to project_id)")
    url: str = Field(..., description="Web link to the page")
    author_account_id: Optional[str] = Field(default=None)
    created: datetime
    updated: datetime
    labels: List[str] = Field(default_factory=list)

class IngestConfluenceBatchRequest(BaseModel):
    """Batch ingestion request for Confluence."""
    pages: List[ConfluencePage] = Field(
        ..., 
        min_length=1, 
        max_length=50
    )
    tenant_id: str = Field(..., description="Tenant identifier for ACL")
    force_update: bool = False


class IngestResponse(BaseModel):
    """Response for async ingestion."""
    job_id: str = Field(..., description="Background job tracking ID")
    status: str = Field(default="accepted", description="Job status")
    message: str = Field(..., description="Status message")
    estimated_completion_seconds: Optional[int] = Field(default=None)


# ===========================================
# Auth Schemas
# ===========================================

class UserContext(BaseModel):
    """Authenticated user context from JWT."""
    account_id: str = Field(..., description="Atlassian account ID")
    email: Optional[str] = Field(default=None)
    display_name: Optional[str] = Field(default=None)
    tenant_id: str = Field(..., description="Atlassian site/tenant ID")
    project_access: List[str] = Field(
        default_factory=list,
        description="List of project IDs/keys user can access"
    )
    roles: List[str] = Field(default_factory=list, description="User roles")
    
    @property
    def acl_filter(self) -> List[str]:
        """Generate ACL filter for vector search."""
        filters = [f"tenant:{self.tenant_id}"]
        filters.extend([f"proj:{p}" for p in self.project_access])
        filters.append(f"user:{self.account_id}")
        return filters


# ===========================================
# Usage/Billing Schemas
# ===========================================

class UsageRecord(BaseModel):
    """Token usage record for billing."""
    tenant_id: str
    user_account_id: str
    timestamp: datetime = Field(default_factory=utc_now)
    operation: str = Field(..., pattern=r'^(query|ingest)$')
    input_tokens: int = Field(..., ge=0)
    output_tokens: int = Field(..., ge=0)
    model: str
    cached: bool = Field(default=False)
    
    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


# ===========================================
# Health Check Schemas
# ===========================================

class HealthStatus(BaseModel):
    """Service health check response."""
    status: str = Field(..., pattern=r'^(healthy|degraded|unhealthy)$')
    version: str
    timestamp: datetime = Field(default_factory=utc_now)
    dependencies: Dict[str, str] = Field(default_factory=dict)
