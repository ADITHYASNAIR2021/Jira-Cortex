"""
Jira Cortex - Query Router

RAG query endpoint with ACL filtering, caching, billing, and rate limiting.
"""

import re
import time
import uuid
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from slowapi import Limiter
from slowapi.util import get_remote_address
import structlog

from app.config import get_settings
from app.models.schemas import (
    QueryRequest, 
    QueryResponse, 
    ErrorResponse,
    Citation,
    UserContext
)

from app.auth.dependencies import get_current_user
from app.services.cache import get_cache_service, CacheService
from app.services.llm import get_llm_service, LLMService, LLMServiceError
from app.services.vector_store import get_vector_store, VectorStore, VectorStoreError
from app.services.billing import get_billing_service, BillingService

logger = structlog.get_logger(__name__)

router = APIRouter(prefix="/api/v1", tags=["query"])

# Rate limiter - use same instance from main app
limiter = Limiter(key_func=get_remote_address)





@router.post(
    "/query",
    response_model=QueryResponse,
    responses={
        401: {"model": ErrorResponse, "description": "Authentication failed"},
        429: {"model": ErrorResponse, "description": "Rate limit exceeded"},
        500: {"model": ErrorResponse, "description": "Internal server error"}
    },
    summary="Query the knowledge base",
    description="Ask questions about your Jira issues and get AI-powered answers with citations."
)
@limiter.limit("60/minute")
async def query(
    request: Request,  # Required for rate limiter
    query_request: QueryRequest,
    user: UserContext = Depends(get_current_user),
    cache_service: CacheService = Depends(get_cache_service),
    llm_service: LLMService = Depends(get_llm_service),
    vector_store: VectorStore = Depends(get_vector_store),
    billing_service: BillingService = Depends(get_billing_service)
) -> QueryResponse:
    """
    Process a natural language query using RAG.
    
    Flow:
    1. Check cache for duplicate query
    2. Generate query embedding
    3. Search vector DB with ACL filter
    4. Generate answer with citations
    5. Record billing usage  ← FIXED
    6. Cache and return
    
    Security:
    - User authenticated via JWT
    - Rate limited to 60/minute
    - Vector search filtered by user's project access
    - Cache keys include ACL to prevent cross-user leakage
    """
    start_time = time.time()
    request_id = str(uuid.uuid4())[:8]
    
    logger.info(
        "query_received",
        request_id=request_id,
        user_id=user.account_id,
        tenant_id=user.tenant_id,
        query_length=len(query_request.query)
    )
    
    # CRITICAL: Step 0 - Check tenant subscription (prevents "free lunch" abuse)
    if not await billing_service.is_tenant_allowed(user.tenant_id):
        logger.warning(
            "tenant_not_allowed",
            request_id=request_id,
            tenant_id=user.tenant_id
        )
        raise HTTPException(
            status_code=403,
            detail={
                "error": "SUBSCRIPTION_REQUIRED",
                "message": "Your organization is not subscribed to Jira Cortex. Please contact support."
            }
        )
    
    # CRITICAL: Step 0b - Check wallet has funds ("No Cash, No Query")
    estimated_query_cost = 0.005  # ~$0.005 per query estimate
    if not await billing_service.has_sufficient_funds(user.tenant_id, estimated_query_cost):
        logger.warning(
            "insufficient_funds",
            request_id=request_id,
            tenant_id=user.tenant_id
        )
        raise HTTPException(
            status_code=402,  # Payment Required
            detail={
                "error": "INSUFFICIENT_CREDITS",
                "message": "Insufficient AI credits. Please top up your balance."
            }
        )
    
    # Check if user has any project access
    if not user.project_access:
        logger.warning("no_project_access", user_id=user.account_id)
        return QueryResponse(
            answer="I couldn't find relevant information in your accessible projects. You may not have access to any projects.",
            confidence_score=0.0,
            citations=[],
            cached=False,
            processing_time_ms=int((time.time() - start_time) * 1000)
        )
    
    try:
        # Step 1: Check cache
        cached_response = await cache_service.get_cached_response(
            query=query_request.query,
            tenant_id=user.tenant_id,
            project_access=user.project_access,
            session_id=query_request.session_id
        )
        
        if cached_response:
            cached_response.processing_time_ms = int((time.time() - start_time) * 1000)
            logger.info("cache_hit", request_id=request_id)
            
            # FIXED: Record cached query usage (reduced billing for cached)
            await billing_service.record_usage(
                tenant_id=user.tenant_id,
                user_account_id=user.account_id,
                operation="query",
                input_tokens=0,
                output_tokens=0,
                model="cache",
                cached=True
            )
            
            # Deduct minimal cost for cached query ($0.001)
            await billing_service.deduct_balance(
                tenant_id=user.tenant_id,
                cost=0.001,
                description=f"Cached query: {request_id}"
            )
            
            return cached_response
        
        # Step 2: Generate or retrieve cached query embedding
        query_embedding = await cache_service.get_cached_embedding(query_request.query)
        embedding_tokens = 0
        if query_embedding is None:
            query_embedding, embedding_tokens = await llm_service.generate_embedding_with_usage(
                query_request.query
            )
            # Cache for 1 hour — query phrasing rarely changes
            await cache_service.cache_embedding(query_request.query, query_embedding, ttl_seconds=3600)
        else:
            logger.debug("embedding_cache_hit", request_id=request_id)
        
        # Step 3: Search with ACL filter
        settings = get_settings()
        search_results = await vector_store.search(
            query_embedding=query_embedding,
            tenant_id=user.tenant_id,
            project_access=user.project_access,
            limit=settings.max_chunks_per_query
        )
        
        # Step 4: Generate answer
        additional_context = None
        if query_request.context:
            # Include current issue context if provided
            additional_context = str(query_request.context)
        
        # Extra Step: Grab Conversation History
        conversation_history = None
        if query_request.session_id:
            conversation_history = await cache_service.get_conversation_history(
                session_id=query_request.session_id,
                tenant_id=user.tenant_id
            )
        
        llm_response = await llm_service.generate_answer(
            query=query_request.query,
            search_results=search_results,
            additional_context=additional_context,
            conversation_history=conversation_history
        )
        
        # Save conversation history to memory
        if query_request.session_id:
            await cache_service.add_to_conversation_history(
                session_id=query_request.session_id,
                tenant_id=user.tenant_id,
                role="user",
                content=query_request.query
            )
            await cache_service.add_to_conversation_history(
                session_id=query_request.session_id,
                tenant_id=user.tenant_id,
                role="assistant",
                content=llm_response.answer
            )
        
        # Build citations
        citations = []
        seen_keys = set()
        for result in search_results:
            if result.issue_key not in seen_keys:
                citations.append(Citation(
                    issue_key=result.issue_key,
                    title=result.issue_title,
                    url=result.url,
                    relevance_score=result.score
                ))
                seen_keys.add(result.issue_key)
        
        # Build response
        processing_time_ms = int((time.time() - start_time) * 1000)
        
        response = QueryResponse(
            answer=llm_response.answer,
            confidence_score=llm_response.confidence_score,
            citations=citations,
            cached=False,
            processing_time_ms=processing_time_ms,
            tokens_used=llm_response.total_tokens + embedding_tokens
        )
        
        # FIXED: Step 5 - Record billing usage (for analytics)
        await billing_service.record_usage(
            tenant_id=user.tenant_id,
            user_account_id=user.account_id,
            operation="query",
            input_tokens=llm_response.input_tokens + embedding_tokens,
            output_tokens=llm_response.output_tokens,
            model=llm_response.model,
            cached=False
        )
        
        # CRITICAL: Step 5b - Deduct actual cost from wallet
        actual_cost = billing_service.calculate_query_cost(
            llm_response.input_tokens + embedding_tokens,
            llm_response.output_tokens
        )
        await billing_service.deduct_balance(
            tenant_id=user.tenant_id,
            cost=actual_cost,
            description=f"Query: {request_id}"
        )
        
        # Step 6: Cache response
        await cache_service.cache_response(
            query=query_request.query,
            tenant_id=user.tenant_id,
            project_access=user.project_access,
            response=response,
            session_id=query_request.session_id
        )
        
        logger.info(
            "query_completed",
            request_id=request_id,
            processing_time_ms=processing_time_ms,
            citations_count=len(citations),
            confidence=llm_response.confidence_score,
            tokens_billed=llm_response.total_tokens + embedding_tokens
        )
        
        return response
        
    except LLMServiceError as e:
        logger.error("llm_error", request_id=request_id, error=str(e))
        raise HTTPException(
            status_code=500,
            detail={
                "error": "LLM_ERROR",
                "message": "Failed to generate answer. Please try again.",
                "request_id": request_id
            }
        )
    
    except VectorStoreError as e:
        logger.error("vector_store_error", request_id=request_id, error=str(e))
        raise HTTPException(
            status_code=500,
            detail={
                "error": "SEARCH_ERROR",
                "message": "Failed to search knowledge base. Please try again.",
                "request_id": request_id
            }
        )
    
    except Exception as e:
        logger.exception("query_failed", request_id=request_id, error=str(e))
        raise HTTPException(
            status_code=500,
            detail={
                "error": "INTERNAL_ERROR",
                "message": "An unexpected error occurred.",
                "request_id": request_id
            }
        )


# =============================================================
# New Feature Endpoints
# =============================================================

class FeedbackRequest(BaseModel):
    """User feedback on a query response."""
    query_hash: str = Field(..., description="Hash from query response (for lookup)")
    vote: int = Field(..., ge=-1, le=1, description="-1=bad, 0=neutral, 1=good")
    comment: Optional[str] = Field(default=None, max_length=500)


@router.post("/feedback", tags=["query"])
@limiter.limit("60/minute")
async def submit_feedback(
    request: Request,
    feedback: FeedbackRequest,
    user: UserContext = Depends(get_current_user),
    cache_service: CacheService = Depends(get_cache_service)
):
    """
    Submit 👍/👎 feedback on a RAG response.
    Stores anonymized feedback for building a fine-tuning dataset.
    """
    import json
    import time
    record = {
        "query_hash": feedback.query_hash,
        "vote": feedback.vote,
        "comment": feedback.comment,
        "tenant_id": user.tenant_id,
        "timestamp": time.time()
    }
    try:
        client = await cache_service.get_client()
        feedback_key = f"cortex:feedback:{user.tenant_id}"
        await client.lpush(feedback_key, json.dumps(record))
        await client.ltrim(feedback_key, 0, 9999)          # Keep last 10k
        await client.expire(feedback_key, 86400 * 90)     # 90-day retention
        logger.info("feedback_recorded", tenant_id=user.tenant_id, vote=feedback.vote)
        return {"status": "recorded", "message": "Thank you for your feedback!"}
    except Exception as e:
        logger.warning("feedback_store_failed", error=str(e))
        return {"status": "accepted", "message": "Feedback received"}


@router.get("/similar", tags=["query"])
@limiter.limit("30/minute")
async def find_similar_issues(
    request: Request,
    issue_key: str,
    limit: int = 5,
    threshold: float = 0.80,
    user: UserContext = Depends(get_current_user),
    vector_store: VectorStore = Depends(get_vector_store)
):
    """
    Find issues semantically similar to the given issue key (duplicate detection).
    Uses the stored vector embeddings to find related issues in the same tenant.
    """
    if not re.match(r'^[A-Z]+-\d+$', issue_key):
        raise HTTPException(status_code=400, detail="Invalid issue key format (e.g. PROJ-123)")

    results = await vector_store.search_similar(
        issue_key=issue_key,
        tenant_id=user.tenant_id,
        project_access=user.project_access,
        limit=limit,
        score_threshold=threshold
    )
    return {
        "source_issue": issue_key,
        "similar_issues": [
            {
                "issue_key": r.issue_key,
                "title": r.issue_title,
                "url": r.url,
                "similarity_score": round(r.score, 4)
            }
            for r in results
        ],
        "count": len(results)
    }


@router.get("/suggestions", tags=["query"])
@limiter.limit("30/minute")
async def get_query_suggestions(
    request: Request,
    limit: int = 5,
    user: UserContext = Depends(get_current_user),
    cache_service: CacheService = Depends(get_cache_service)
):
    """
    Return top-K most queried questions for the tenant.
    Powers the smart query suggestion chips in the OmniSearch UI.
    """
    try:
        client = await cache_service.get_client()
        suggestion_key = f"cortex:suggestions:{user.tenant_id}"
        raw = await client.zrevrange(suggestion_key, 0, limit - 1, withscores=True)
        suggestions = [
            {"query": q.decode() if isinstance(q, bytes) else q, "count": int(s)}
            for q, s in raw
        ]
        return {"suggestions": suggestions}
    except Exception as e:
        logger.warning("suggestions_fetch_failed", error=str(e))
        return {"suggestions": []}
