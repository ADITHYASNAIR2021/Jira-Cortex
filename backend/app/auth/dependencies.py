from fastapi import Header, HTTPException
import structlog

from app.models.schemas import UserContext
from app.auth.jwt_validator import get_jwt_validator, JWTValidationError

logger = structlog.get_logger(__name__)

async def get_current_user(
    authorization: str = Header(..., description="JWT Bearer token")
) -> UserContext:
    """
    Dependency to validate JWT and extract user context.
    
    Security: All queries require valid authentication.
    """
    validator = get_jwt_validator()
    
    try:
        user_context = await validator.validate_token(authorization)
        return user_context
    except JWTValidationError as e:
        logger.warning("auth_failed", error=e.error_code, message=e.message)
        raise HTTPException(
            status_code=401,
            detail={"error": e.error_code, "message": e.message}
        )
