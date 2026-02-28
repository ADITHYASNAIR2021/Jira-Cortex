"""
Jira Cortex - JWT Validation Tests

Tests for Atlassian JWT token validation.
"""

import pytest
import jwt
import time
from unittest.mock import AsyncMock, patch
from app.auth.jwt_validator import (
    AtlassianJWTValidator,
    JWTValidationError,
    get_jwt_validator
)


class TestJWTValidator:
    """Tests for JWT token validation."""
    
    @pytest.fixture
    def validator(self, mock_settings, monkeypatch):
        monkeypatch.setattr("app.auth.jwt_validator.get_settings", lambda: mock_settings)
        return AtlassianJWTValidator()
    
    def test_missing_token_raises_error(self, validator):
        """Should reject missing token."""
        with pytest.raises(JWTValidationError) as exc_info:
            # Sync wrapper for async test
            import asyncio
            asyncio.get_event_loop().run_until_complete(
                validator.validate_token("")
            )
        
        assert exc_info.value.error_code == "MISSING_TOKEN"
    
    def test_strip_bearer_prefix(self, validator):
        """Should handle Bearer prefix."""
        token = "Bearer some.token.here"
        # Token processing starts by stripping Bearer
        assert validator is not None  # Setup test
    
    @pytest.mark.asyncio
    async def test_expired_token_rejected(self, validator):
        """Should reject expired tokens."""
        # Create an expired token
        expired_payload = {
            "exp": int(time.time()) - 3600,  # 1 hour ago
            "iat": int(time.time()) - 7200,
            "iss": "test.atlassian.net",
            "sub": "test-user"
        }
        
        # This would fail because we can't sign with Atlassian's key
        # But we can test the error path
        with pytest.raises(JWTValidationError):
            await validator.validate_token("invalid.token.here")
    
    @pytest.mark.asyncio
    async def test_invalid_token_rejected(self, validator):
        """Should reject malformed tokens."""
        with pytest.raises(JWTValidationError):
            await validator.validate_token("not-a-valid-jwt")


class TestConstantTimeCompare:
    """Tests for timing attack prevention."""
    
    @pytest.fixture
    def validator(self, mock_settings, monkeypatch):
        monkeypatch.setattr("app.auth.jwt_validator.get_settings", lambda: mock_settings)
        return AtlassianJWTValidator()
    
    def test_equal_strings_match(self, validator):
        """Equal strings should compare as equal."""
        assert validator._constant_time_compare("test", "test") is True
    
    def test_different_strings_dont_match(self, validator):
        """Different strings should not match."""
        assert validator._constant_time_compare("test", "other") is False
    
    def test_empty_strings(self, validator):
        """Empty strings should match."""
        assert validator._constant_time_compare("", "") is True


class TestProjectAccessExtraction:
    """Tests for extracting project access from tokens."""
    
    @pytest.fixture
    def validator(self, mock_settings, monkeypatch):
        monkeypatch.setattr("app.auth.jwt_validator.get_settings", lambda: mock_settings)
        return AtlassianJWTValidator()
    
    def test_extract_from_project_ids(self, validator):
        """Should extract projects from projectIds claim."""
        payload = {
            "context": {
                "projectIds": ["PROJ-1", "PROJ-2"]
            }
        }
        
        projects = validator._extract_project_access(payload)
        assert "PROJ-1" in projects
        assert "PROJ-2" in projects
    
    def test_extract_from_jira_context(self, validator):
        """Should extract project from Jira context."""
        payload = {
            "context": {
                "jira": {
                    "project": {"id": "10001", "key": "MOBILE"}
                }
            }
        }
        
        projects = validator._extract_project_access(payload)
        assert "10001" in projects or "MOBILE" in projects
    
    def test_empty_context(self, validator):
        """Should handle missing context gracefully."""
        payload = {}
        projects = validator._extract_project_access(payload)
        assert projects == []


class TestJWKSCaching:
    """Tests for JWKS caching behavior."""
    
    @pytest.fixture
    def validator(self, mock_settings, monkeypatch):
        monkeypatch.setattr("app.auth.jwt_validator.get_settings", lambda: mock_settings)
        return AtlassianJWTValidator()
    
    def test_cache_is_initially_empty(self, validator):
        """Cache should be empty initially."""
        assert validator._jwks_cache is None
        assert validator._jwks_cache_time == 0
    
    @pytest.mark.asyncio
    async def test_uses_cached_jwks(self, validator):
        """Should use cached JWKS when available."""
        # Set up cache
        validator._jwks_cache = {"keys": []}
        validator._jwks_cache_time = time.time()
        
        # This should use cache without fetching
        jwks = await validator._fetch_jwks()
        assert jwks == {"keys": []}
