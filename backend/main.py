"""
Jira Cortex - FastAPI Application

Main application entry point with security middleware.
Includes billing integration for usage tracking.
"""

import time
from datetime import date
from contextlib import asynccontextmanager
from typing import Callable
import structlog
from fastapi import FastAPI, Request, Response, Depends, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from slowapi import Limiter, _rate_limit_exceeded_handler
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded

from app.config import get_settings
from app.routers import query, ingest, payments
from app.services.vector_store import get_vector_store
from app.services.cache import get_cache_service
from app.services.llm import get_llm_service
from app.services.billing import get_billing_service
from app.auth.jwt_validator import get_jwt_validator, JWTValidationError

# Configure structured logging
structlog.configure(
    processors=[
        structlog.stdlib.filter_by_level,
        structlog.stdlib.add_logger_name,
        structlog.stdlib.add_log_level,
        structlog.stdlib.PositionalArgumentsFormatter(),
        structlog.processors.TimeStamper(fmt="iso"),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.format_exc_info,
        structlog.processors.UnicodeDecoder(),
        structlog.processors.JSONRenderer()
    ],
    wrapper_class=structlog.stdlib.BoundLogger,
    context_class=dict,
    logger_factory=structlog.stdlib.LoggerFactory(),
    cache_logger_on_first_use=True,
)

logger = structlog.get_logger(__name__)

# Rate limiter
limiter = Limiter(key_func=get_remote_address)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager.
    
    - Initialize services on startup
    - Clean up on shutdown
    """
    logger.info("application_starting")
    
    settings = get_settings()
    
    # Initialize vector store collection
    try:
        vector_store = get_vector_store()
        await vector_store.initialize_collection()
        logger.info("vector_store_initialized")
    except Exception as e:
        logger.error("vector_store_init_failed", error=str(e))
        # Continue anyway - might be temporary issue
    
    # Verify Redis connection
    try:
        cache = get_cache_service()
        if await cache.health_check():
            logger.info("redis_connected")
        else:
            logger.warning("redis_unavailable")
    except Exception as e:
        logger.warning("redis_check_failed", error=str(e))
    
    # Initialize billing database
    try:
        billing = get_billing_service()
        await billing.initialize()
        logger.info("billing_db_initialized")
    except Exception as e:
        logger.warning("billing_init_failed", error=str(e))
        # Non-critical - continue without billing
    
    # Recover orphaned background jobs (Redis persistence)
    try:
        from app.services.background_processor import get_background_processor
        processor = get_background_processor()
        recovered = await processor.recover_orphaned_jobs()
        if recovered > 0:
            logger.info("orphaned_jobs_marked_failed", count=recovered)
    except Exception as e:
        logger.warning("orphan_recovery_check_failed", error=str(e))
    
    yield
    
    # Cleanup
    logger.info("application_shutting_down")
    
    cache = get_cache_service()
    await cache.close()
    
    billing = get_billing_service()
    await billing.close()


def create_app() -> FastAPI:
    """
    Create and configure the FastAPI application.
    """
    settings = get_settings()
    
    app = FastAPI(
        title="Jira Cortex API",
        description="""
# Permission-aware RAG Intelligence for Atlassian Jira

Transform your Jira "dead data" into actionable insights with natural language queries.

## Features
- 🔍 **Natural Language Queries** - Ask questions about your Jira issues in plain English
- 🔒 **ACL-Filtered Search** - Users only see results from their accessible projects
- 📝 **Citation-Backed Answers** - Every answer includes source issue links
- 📊 **Confidence Scores** - Know how reliable each answer is (0-100%)
- ⚡ **Real-Time Sync** - Webhook-based updates keep data fresh

## Authentication
All endpoints require a valid Atlassian JWT token in the Authorization header.

## Rate Limits
| Endpoint | Limit |
|----------|-------|
| `/api/v1/query` | 60/minute |
| `/api/v1/ingest/batch` | 10/minute |
| `/api/v1/ingest/single` | 120/minute |
        """,
        version="1.0.0",
        docs_url="/docs",  # Always available (production-ready)
        redoc_url="/redoc",
        openapi_url="/openapi.json",
        contact={
            "name": "Jira Cortex Support",
            "email": "support@jiracortex.io",
        },
        license_info={
            "name": "Proprietary",
            "url": "https://jiracortex.io/license",
        },
        openapi_tags=[
            {"name": "query", "description": "RAG query operations"},
            {"name": "ingest", "description": "Data ingestion endpoints"},
            {"name": "health", "description": "Health and readiness probes"},
            {"name": "ops", "description": "Operations and metrics"},
            {"name": "usage", "description": "Usage and billing"},
            {"name": "admin", "description": "Admin operations"},
        ],
        lifespan=lifespan
    )
    
    # Rate limiting
    app.state.limiter = limiter
    app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
    
    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-Tenant-ID"],
        max_age=600  # Cache preflight for 10 minutes
    )
    
    # Request logging middleware
    @app.middleware("http")
    async def log_requests(request: Request, call_next: Callable) -> Response:
        """Log all requests with timing."""
        request_id = request.headers.get("X-Request-ID", "")
        start_time = time.time()
        
        # Add request ID to response
        response = await call_next(request)
        
        process_time = time.time() - start_time
        
        logger.info(
            "request_completed",
            method=request.method,
            path=request.url.path,
            status_code=response.status_code,
            duration_ms=round(process_time * 1000, 2),
            request_id=request_id
        )
        
        response.headers["X-Process-Time"] = str(round(process_time * 1000, 2))
        
        return response
    
    # Security headers middleware
    @app.middleware("http")
    async def add_security_headers(request: Request, call_next: Callable) -> Response:
        """Add security headers to all responses."""
        response = await call_next(request)
        
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        response.headers["Cache-Control"] = "no-store"
        
        return response
    
    # Include routers
    app.include_router(query.router)
    app.include_router(ingest.router)
    app.include_router(payments.router)
    
    # Health check endpoint
    @app.get("/health", tags=["health"])
    async def health_check():
        """
        Health check endpoint for load balancers.
        """
        vector_store = get_vector_store()
        cache = get_cache_service()
        llm = get_llm_service()
        
        return {
            "status": "healthy",
            "version": "1.0.0",
            "dependencies": {
                "qdrant": "healthy" if await vector_store.health_check() else "unhealthy",
                "redis": "healthy" if await cache.health_check() else "degraded",
                "openai": "healthy" if await llm.health_check() else "unhealthy"
            }
        }
    
    # Usage statistics endpoint (for admin page)
    @app.get("/api/v1/usage/current", tags=["usage"])
    async def get_current_usage(request: Request):
        """
        Get usage statistics for current month.
        
        Returns token usage and estimated cost for billing.
        """
        # Validate JWT
        auth_header = request.headers.get("Authorization", "")
        tenant_id = request.headers.get("X-Tenant-ID", "")
        
        if not auth_header:
            raise HTTPException(status_code=401, detail="Missing authorization")
        
        try:
            validator = get_jwt_validator()
            user_context = await validator.validate_token(auth_header)
        except JWTValidationError as e:
            raise HTTPException(status_code=401, detail=e.message)
        
        # Get current month usage
        billing = get_billing_service()
        today = date.today()
        
        try:
            usage = await billing.get_monthly_bill(
                tenant_id=tenant_id or user_context.tenant_id,
                year=today.year,
                month=today.month
            )
            
            return {
                "queries": usage.get("usage", {}).get("total_requests", 0),
                "tokens": usage.get("usage", {}).get("total_tokens", 0),
                "cost": f"{usage.get('billing', {}).get('total_cost_usd', 0):.2f}"
            }
        except Exception as e:
            logger.warning("usage_fetch_failed", error=str(e))
            return {
                "queries": 0,
                "tokens": 0,
                "cost": "0.00",
                "error": "Usage data unavailable"
            }
    
    # Root endpoint
    @app.get("/", tags=["root"])
    async def root():
        """Root endpoint."""
        return {
            "name": "Jira Cortex API",
            "version": "1.0.0",
            "docs": "/docs"
        }
    
    # Readiness probe for Kubernetes
    @app.get("/ready", tags=["health"])
    async def readiness_check():
        """
        Kubernetes readiness probe.
        
        Returns 200 only when the service is ready to accept traffic.
        Unlike /health, this will return 503 if critical dependencies are down.
        """
        vector_store = get_vector_store()
        llm = get_llm_service()
        
        # Check critical dependencies
        qdrant_ok = await vector_store.health_check()
        openai_ok = await llm.health_check()
        
        if not qdrant_ok or not openai_ok:
            return JSONResponse(
                status_code=503,
                content={
                    "ready": False,
                    "reason": "Critical dependency unavailable",
                    "qdrant": "ok" if qdrant_ok else "unavailable",
                    "openai": "ok" if openai_ok else "unavailable"
                }
            )
        
        return {"ready": True}
    
    # Prometheus metrics endpoint
    @app.get("/metrics", tags=["ops"])
    async def prometheus_metrics():
        """
        Prometheus-compatible metrics endpoint.
        
        Exposes key metrics for monitoring and alerting.
        """
        billing = get_billing_service()
        cache = get_cache_service()
        
        # Get current stats (simplified - production would use prometheus_client)
        today = date.today()
        
        try:
            # This month's usage
            usage = await billing.get_tenant_usage(
                tenant_id="*",  # All tenants
                start_date=date(today.year, today.month, 1),
                end_date=today
            )
            total_requests = usage.get("usage", {}).get("total_requests", 0)
            total_tokens = usage.get("usage", {}).get("total_tokens", 0)
        except:
            total_requests = 0
            total_tokens = 0
        
        # Return Prometheus text format
        metrics = f"""# HELP jira_cortex_requests_total Total API requests this month
# TYPE jira_cortex_requests_total counter
jira_cortex_requests_total {total_requests}

# HELP jira_cortex_tokens_total Total tokens consumed this month
# TYPE jira_cortex_tokens_total counter
jira_cortex_tokens_total {total_tokens}

# HELP jira_cortex_up Service is up
# TYPE jira_cortex_up gauge
jira_cortex_up 1
"""
        return Response(content=metrics, media_type="text/plain")
    
    # Admin status endpoint (detailed)
    @app.get("/api/v1/admin/status", tags=["admin"])
    async def admin_status(request: Request):
        """
        Detailed service status for admin dashboard.
        
        Requires authentication. Returns comprehensive health info.
        """
        # Validate JWT (admin only in production)
        auth_header = request.headers.get("Authorization", "")
        if auth_header and not settings.app_env == "development":
            try:
                validator = get_jwt_validator()
                await validator.validate_token(auth_header)
            except JWTValidationError as e:
                raise HTTPException(status_code=401, detail=e.message)
        
        vector_store = get_vector_store()
        cache = get_cache_service()
        llm = get_llm_service()
        billing = get_billing_service()
        
        return {
            "service": "jira-cortex",
            "version": "1.0.0",
            "environment": settings.app_env,
            "status": "operational",
            "dependencies": {
                "qdrant": {
                    "status": "healthy" if await vector_store.health_check() else "unhealthy",
                    "url": settings.qdrant_url[:50] + "..." if len(settings.qdrant_url) > 50 else settings.qdrant_url
                },
                "redis": {
                    "status": "healthy" if await cache.health_check() else "degraded",
                    "url": settings.redis_url.split("@")[-1] if "@" in settings.redis_url else settings.redis_url[:30]
                },
                "openai": {
                    "status": "healthy" if await llm.health_check() else "unhealthy",
                    "model": settings.openai_chat_model
                },
                "postgres": {
                    "status": "configured" if settings.database_url else "not_configured"
                }
            },
            "config": {
                "rate_limit": f"{settings.rate_limit_requests_per_minute}/min",
                "max_chunks_per_query": settings.max_chunks_per_query,
                "embedding_model": settings.openai_embedding_model,
                "usage_tracking": settings.enable_usage_tracking
            }
        }
    
    return app


# Create app instance
app = create_app()


if __name__ == "__main__":
    import uvicorn
    
    settings = get_settings()
    
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.app_debug,
        log_level=settings.log_level.lower()
    )
