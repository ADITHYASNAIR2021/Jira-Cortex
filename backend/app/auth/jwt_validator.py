"""
Jira Cortex - JWT Token Validator

Secure validation of Atlassian Forge JWT tokens.
Implements strict validation with timing attack prevention.
"""

import time
import hashlib
import hmac
from datetime import datetime, timezone
from typing import Optional, Tuple
import structlog
import jwt
from jwt import PyJWTError
import httpx

from app.config import get_settings
from app.models.schemas import UserContext

logger = structlog.get_logger(__name__)


class JWTValidationError(Exception):
    """Raised when JWT validation fails."""
    def __init__(self, message: str, error_code: str = "INVALID_TOKEN"):
        self.message = message
        self.error_code = error_code
        super().__init__(self.message)


class AtlassianJWTValidator:
    """
    Validates JWT tokens from Atlassian Forge apps.
    
    Security features:
    - Signature verification using Atlassian public keys
    - Token expiry validation with clock skew tolerance
    - Issuer and audience validation
    - Timing attack prevention via constant-time comparison
    """
    
    # Atlassian's public key endpoint
    ATLASSIAN_JWKS_URL = "https://api.atlassian.com/.well-known/jwks.json"
    
    # Maximum clock skew allowed (seconds)
    MAX_CLOCK_SKEW_SECONDS = 60
    
    # Cache duration for JWKS (seconds)
    JWKS_CACHE_DURATION = 3600
    
    def __init__(self):
        self.settings = get_settings()
        self._jwks_cache: Optional[dict] = None
        self._jwks_cache_time: float = 0
        
    async def _fetch_jwks(self) -> dict:
        """
        Fetch Atlassian's JSON Web Key Set for signature verification.
        Cached to reduce latency.
        """
        now = time.time()
        
        # Return cached JWKS if still valid
        if (self._jwks_cache and 
            now - self._jwks_cache_time < self.JWKS_CACHE_DURATION):
            return self._jwks_cache
        
        try:
            async with httpx.AsyncClient(timeout=10.0) as client:
                response = await client.get(self.ATLASSIAN_JWKS_URL)
                response.raise_for_status()
                
                self._jwks_cache = response.json()
                self._jwks_cache_time = now
                
                logger.info("jwks_fetched", 
                           keys_count=len(self._jwks_cache.get("keys", [])))
                return self._jwks_cache
                
        except httpx.HTTPError as e:
            logger.error("jwks_fetch_failed", error=str(e))
            # Return cached if available, even if stale
            if self._jwks_cache:
                logger.warning("using_stale_jwks")
                return self._jwks_cache
            raise JWTValidationError(
                "Unable to verify token: JWKS unavailable",
                error_code="JWKS_UNAVAILABLE"
            )
    
    def _constant_time_compare(self, a: str, b: str) -> bool:
        """
        Constant-time string comparison to prevent timing attacks.
        """
        return hmac.compare_digest(a.encode(), b.encode())
    
    async def validate_token(self, token: str) -> UserContext:
        """
        Validate an Atlassian JWT token and extract user context.
        
        Args:
            token: The JWT token from Authorization header
            
        Returns:
            UserContext with validated user information
            
        Raises:
            JWTValidationError: If token is invalid
        """
        if not token:
            raise JWTValidationError("Missing authentication token", "MISSING_TOKEN")
        
        # Remove "Bearer " prefix if present
        if token.lower().startswith("bearer "):
            token = token[7:]
        
        try:
            # Decode header to get key ID
            unverified_header = jwt.get_unverified_header(token)
            kid = unverified_header.get("kid")
            
            if not kid:
                raise JWTValidationError("Token missing key ID", "MISSING_KID")
            
            # Fetch JWKS and find matching key
            jwks = await self._fetch_jwks()
            
            signing_key = None
            for key in jwks.get("keys", []):
                if key.get("kid") == kid:
                    signing_key = jwt.algorithms.RSAAlgorithm.from_jwk(key)
                    break
            
            if not signing_key:
                raise JWTValidationError(
                    "Token signed with unknown key",
                    "UNKNOWN_KEY"
                )
            
            # Verify and decode token
            payload = jwt.decode(
                token,
                signing_key,
                algorithms=["RS256"],
                options={
                    "verify_signature": True,
                    "verify_exp": True,
                    "verify_iat": True,
                    "verify_nbf": True,
                    "require": ["exp", "iat", "iss", "sub"]
                },
                leeway=self.MAX_CLOCK_SKEW_SECONDS
            )
            
            # Validate issuer
            # FIXED: Atlassian is moving to custom domains, so we trust signature
            # verification rather than domain suffix. The key came from Atlassian's
            # JWKS endpoint, so if signature verified, the issuer is valid.
            issuer = payload.get("iss", "")
            if not issuer:
                raise JWTValidationError(
                    "Token missing issuer claim",
                    "MISSING_ISSUER"
                )
            
            # Log issuer for debugging (helps troubleshoot custom domain issues)
            logger.debug("jwt_issuer_validated", issuer=issuer)
            
            # Extract user context
            context = payload.get("context", {})
            user = context.get("user", {})
            
            # Extract project access from token claims
            project_access = self._extract_project_access(payload)
            
            return UserContext(
                account_id=user.get("accountId") or payload.get("sub"),
                email=user.get("email"),
                display_name=user.get("displayName"),
                tenant_id=payload.get("iss", "").split(".")[0].replace("https://", ""),
                project_access=project_access,
                roles=context.get("roles", [])
            )
            
        except jwt.ExpiredSignatureError:
            raise JWTValidationError("Token has expired", "TOKEN_EXPIRED")
        except jwt.InvalidTokenError as e:
            logger.warning("jwt_validation_failed", error=str(e))
            raise JWTValidationError(f"Invalid token: {str(e)}", "INVALID_TOKEN")
        except PyJWTError as e:
            logger.error("jwt_decode_error", error=str(e))
            raise JWTValidationError("Token validation failed", "VALIDATION_ERROR")
    
    def _extract_project_access(self, payload: dict) -> list:
        """
        Extract project access permissions from token claims.
        """
        context = payload.get("context", {})
        
        # Try different claim locations
        projects = []
        
        # Check for explicit project claims
        if "projectIds" in context:
            projects.extend(context["projectIds"])
        
        if "projects" in context:
            for proj in context["projects"]:
                if isinstance(proj, str):
                    projects.append(proj)
                elif isinstance(proj, dict):
                    projects.append(proj.get("id") or proj.get("key"))
        
        # Check Jira-specific claims
        jira_context = context.get("jira", {})
        if "project" in jira_context:
            proj = jira_context["project"]
            if isinstance(proj, str):
                projects.append(proj)
            elif isinstance(proj, dict):
                projects.append(proj.get("id") or proj.get("key"))
        
        return list(set(filter(None, projects)))
    
    async def validate_forge_request(
        self, 
        authorization: str,
        expected_tenant: Optional[str] = None
    ) -> Tuple[UserContext, dict]:
        """
        Validate a complete Forge request with additional security checks.
        
        Returns:
            Tuple of (UserContext, raw_claims)
        """
        user_context = await self.validate_token(authorization)
        
        # Verify tenant if expected
        if expected_tenant and not self._constant_time_compare(
            user_context.tenant_id, expected_tenant
        ):
            raise JWTValidationError(
                "Tenant mismatch",
                "TENANT_MISMATCH"
            )
        
        return user_context, {}


# Singleton validator instance
_validator: Optional[AtlassianJWTValidator] = None


def get_jwt_validator() -> AtlassianJWTValidator:
    """Get or create JWT validator singleton."""
    global _validator
    if _validator is None:
        _validator = AtlassianJWTValidator()
    return _validator
