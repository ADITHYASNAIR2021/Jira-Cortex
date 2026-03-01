"""
Jira Cortex - FastAPI Application

Main application entry point with security middleware.
Includes billing integration for usage tracking.
"""

import asyncio
import time
import uuid
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
from app.services.vector_store import VectorStore
from app.services.cache import CacheService
from app.services.llm import LLMService
from app.services.billing import BillingService
from app.services.background_processor import BackgroundProcessor
from app.auth.jwt_validator import get_jwt_validator, JWTValidationError
import sentry_sdk

settings = get_settings()

# Initialize Sentry only if DSN is configured
if settings.sentry_dsn:
    sentry_sdk.init(
        dsn=settings.sentry_dsn,
        traces_sample_rate=0.2,
        environment=settings.app_env,
    )

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

# Rate limiter - keyed by tenant ID when available, fallback to IP
def get_rate_limit_key(request: Request) -> str:
    """Rate limit by tenant ID from header, falling back to IP."""
    tenant_id = request.headers.get("X-Tenant-ID", "")
    if tenant_id:
        return f"tenant:{tenant_id}"
    return get_remote_address(request)

limiter = Limiter(key_func=get_rate_limit_key)


async def _periodic_job_cleanup(processor: BackgroundProcessor) -> None:
    """Periodic cleanup task to remove stale in-memory jobs."""
    while True:
        try:
            await asyncio.sleep(3600)  # Run every hour
            cleaned = processor.cleanup_old_jobs(max_age_seconds=3600)
            if cleaned > 0:
                logger.info("periodic_job_cleanup", cleaned=cleaned)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.warning("job_cleanup_error", error=str(e))


@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Application lifespan manager.

    - Initialize services on startup
    - Clean up on shutdown
    """
    logger.info("application_starting")

    # Initialize App State Singletons
    app.state.vector_store = VectorStore()
    app.state.cache_service = CacheService()
    app.state.llm_service = LLMService()
    app.state.billing_service = BillingService()
    app.state.background_processor = BackgroundProcessor(
        vector_store=app.state.vector_store,
        llm_service=app.state.llm_service,
        billing_service=app.state.billing_service,
        cache_service=app.state.cache_service,
    )

    # Initialize vector store collection
    try:
        await app.state.vector_store.initialize_collection()
        logger.info("vector_store_initialized")
    except Exception as e:
        logger.error("vector_store_init_failed", error=str(e))
        # Continue anyway - might be temporary issue

    # Verify Redis connection
    try:
        if await app.state.cache_service.health_check():
            logger.info("redis_connected")
        else:
            logger.warning("redis_unavailable")
    except Exception as e:
        logger.warning("redis_check_failed", error=str(e))

    # Initialize billing database
    try:
        await app.state.billing_service.initialize()
        logger.info("billing_db_initialized")
    except Exception as e:
        logger.warning("billing_init_failed", error=str(e))
        # Non-critical - continue without billing

    # Recover orphaned background jobs (Redis persistence)
    try:
        recovered = await app.state.background_processor.recover_orphaned_jobs()
        if recovered > 0:
            logger.info("orphaned_jobs_marked_failed", count=recovered)
    except Exception as e:
        logger.warning("orphan_recovery_check_failed", error=str(e))

    # Start periodic in-memory job cleanup
    cleanup_task = asyncio.create_task(
        _periodic_job_cleanup(app.state.background_processor)
    )

    yield

    # Cleanup
    logger.info("application_shutting_down")
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass

    await app.state.cache_service.close()
    await app.state.billing_service.close()


def create_app() -> FastAPI:
    """
    Create and configure the FastAPI application.
    """
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
        # Docs only available in non-production environments for security
        docs_url="/docs" if settings.app_env != "production" else None,
        redoc_url="/redoc" if settings.app_env != "production" else None,
        openapi_url="/openapi.json" if settings.app_env != "production" else None,
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

    # Global exception handler
    @app.exception_handler(Exception)
    async def global_exception_handler(request: Request, exc: Exception):
        logger.error(
            "unhandled_exception",
            method=request.method,
            url=str(request.url),
            error=str(exc)
        )
        return JSONResponse(
            status_code=500,
            content={"detail": "An unexpected internal server error occurred. Please contact support."}
        )

    # CORS middleware
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins_list,
        allow_credentials=True,
        allow_methods=["GET", "POST", "DELETE", "OPTIONS"],
        allow_headers=["Authorization", "Content-Type", "X-Request-ID", "X-Tenant-ID"],
        max_age=600  # Cache preflight for 10 minutes
    )

    # Request logging middleware
    @app.middleware("http")
    async def log_requests(request: Request, call_next: Callable) -> Response:
        """Log all requests with timing."""
        request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
        start_time = time.time()

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
        response.headers["X-Request-ID"] = request_id

        return response

    # Timeout middleware
    @app.middleware("http")
    async def timeout_middleware(request: Request, call_next: Callable) -> Response:
        try:
            return await asyncio.wait_for(call_next(request), timeout=45.0)
        except asyncio.TimeoutError:
            return JSONResponse({"error": "REQUEST_TIMEOUT"}, status_code=504)

    # Request size limit middleware
    @app.middleware("http")
    async def limit_content_length(request: Request, call_next: Callable) -> Response:
        """Limit max request payload size to 10MB."""
        content_length = request.headers.get("content-length")
        if content_length and int(content_length) > 10_000_000:
            return JSONResponse(status_code=413, content={"detail": "Payload too large"})
        return await call_next(request)

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
        Lightweight health check for load balancers.
        Always returns 200 if the process is alive.
        """
        return {
            "status": "healthy",
            "version": "1.0.0",
            "environment": settings.app_env,
        }

    @app.get("/health/deep", tags=["health"])
    async def deep_health_check(request: Request):
        """
        Deep health check checking dependencies.
        """
        vector_store: VectorStore = request.app.state.vector_store
        cache: CacheService = request.app.state.cache_service
        llm: LLMService = request.app.state.llm_service

        qdrant_ok = await vector_store.health_check()
        redis_ok = await cache.health_check()
        openai_ok = await llm.health_check()

        overall = "healthy" if (qdrant_ok and openai_ok) else "degraded" if redis_ok else "unhealthy"

        return {
            "status": overall,
            "version": "1.0.0",
            "dependencies": {
                "qdrant": "healthy" if qdrant_ok else "unhealthy",
                "redis": "healthy" if redis_ok else "degraded",
                "openai": "healthy" if openai_ok else "unhealthy"
            }
        }

    # Readiness probe for Kubernetes
    @app.get("/ready", tags=["health"])
    async def readiness_check(request: Request):
        """
        Kubernetes readiness probe.

        Returns 200 only when the service is ready to accept traffic.
        Returns 503 if critical dependencies are down.
        """
        vector_store: VectorStore = request.app.state.vector_store
        llm: LLMService = request.app.state.llm_service

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
        billing: BillingService = request.app.state.billing_service
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
            "environment": settings.app_env,
        }

    # Prometheus metrics endpoint
    @app.get("/metrics", tags=["ops"])
    async def prometheus_metrics(request: Request):
        """
        Prometheus-compatible metrics endpoint.

        Exposes key metrics for monitoring and alerting.
        Requires auth in production.
        """
        # Require auth in production to protect business metrics
        if settings.app_env == "production":
            auth_header = request.headers.get("Authorization", "")
            if not auth_header:
                raise HTTPException(status_code=401, detail="Metrics require authentication in production")
            try:
                validator = get_jwt_validator()
                await validator.validate_token(auth_header)
            except JWTValidationError as e:
                raise HTTPException(status_code=401, detail=e.message)

        billing: BillingService = request.app.state.billing_service
        cache: CacheService = request.app.state.cache_service
        vector_store: VectorStore = request.app.state.vector_store

        today = date.today()

        try:
            usage = await billing.get_tenant_usage(
                tenant_id="*",  # All tenants
                start_date=date(today.year, today.month, 1),
                end_date=today
            )
            total_requests = usage.get("usage", {}).get("total_requests", 0)
            total_tokens = usage.get("usage", {}).get("total_tokens", 0)
        except Exception:
            total_requests = 0
            total_tokens = 0

        qdrant_up = 1 if await vector_store.health_check() else 0
        redis_up = 1 if await cache.health_check() else 0

        metrics = f"""# HELP jira_cortex_requests_total Total API requests this month
# TYPE jira_cortex_requests_total counter
jira_cortex_requests_total {total_requests}

# HELP jira_cortex_tokens_total Total tokens consumed this month
# TYPE jira_cortex_tokens_total counter
jira_cortex_tokens_total {total_tokens}

# HELP jira_cortex_up Service is up
# TYPE jira_cortex_up gauge
jira_cortex_up 1

# HELP jira_cortex_qdrant_up Qdrant vector store is up
# TYPE jira_cortex_qdrant_up gauge
jira_cortex_qdrant_up {qdrant_up}

# HELP jira_cortex_redis_up Redis cache is up
# TYPE jira_cortex_redis_up gauge
jira_cortex_redis_up {redis_up}
"""
        return Response(content=metrics, media_type="text/plain")

    # Admin status endpoint (detailed)
    @app.get("/api/v1/admin/status", tags=["admin"])
    async def admin_status(request: Request):
        """
        Detailed service status for admin dashboard.

        Requires authentication. Returns comprehensive health info.
        """
        # Always require auth - only skip in development
        if settings.app_env != "development":
            auth_header = request.headers.get("Authorization", "")
            if not auth_header:
                raise HTTPException(status_code=401, detail="Missing authorization")
            try:
                validator = get_jwt_validator()
                await validator.validate_token(auth_header)
            except JWTValidationError as e:
                raise HTTPException(status_code=401, detail=e.message)

        vector_store: VectorStore = request.app.state.vector_store
        cache: CacheService = request.app.state.cache_service
        llm: LLMService = request.app.state.llm_service
        billing: BillingService = request.app.state.billing_service

        qdrant_ok = await vector_store.health_check()
        redis_ok = await cache.health_check()
        openai_ok = await llm.health_check()

        return {
            "service": "jira-cortex",
            "version": "1.0.0",
            "environment": settings.app_env,
            "status": "operational",
            "dependencies": {
                "qdrant": {
                    "status": "healthy" if qdrant_ok else "unhealthy",
                    "url": settings.qdrant_url[:50] + "..." if len(settings.qdrant_url) > 50 else settings.qdrant_url
                },
                "redis": {
                    "status": "healthy" if redis_ok else "degraded",
                    "url": settings.redis_url.split("@")[-1] if "@" in settings.redis_url else settings.redis_url[:30]
                },
                "openai": {
                    "status": "healthy" if openai_ok else "unhealthy",
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

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=settings.app_debug,
        log_level=settings.log_level.lower()
    )
