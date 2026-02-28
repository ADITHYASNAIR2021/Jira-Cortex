"""
Jira Cortex - Tenant Gating Tests

Tests for tenant subscription validation.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestTenantGating:
    """Tests for tenant gating / subscription validation."""
    
    @pytest.fixture
    def mock_settings_dev(self):
        """Settings for development mode (allow all)."""
        settings = MagicMock()
        settings.app_env = "development"
        settings.allowed_tenants = ""
        settings.enable_usage_tracking = True
        settings.database_url = None
        return settings
    
    @pytest.fixture
    def mock_settings_prod_with_whitelist(self):
        """Settings for production mode with tenant whitelist."""
        settings = MagicMock()
        settings.app_env = "production"
        settings.allowed_tenants = "tenant-abc,tenant-xyz"
        settings.enable_usage_tracking = True
        settings.database_url = None
        return settings
    
    @pytest.fixture
    def mock_settings_prod_empty_whitelist(self):
        """Settings for production mode with empty whitelist (deny all)."""
        settings = MagicMock()
        settings.app_env = "production"
        settings.allowed_tenants = ""
        settings.enable_usage_tracking = True
        settings.database_url = None
        return settings
    
    @pytest.mark.asyncio
    async def test_dev_mode_allows_all_tenants(self, mock_settings_dev, monkeypatch):
        """Development mode should allow any tenant."""
        monkeypatch.setattr("app.services.billing.get_settings", lambda: mock_settings_dev)
        
        from app.services.billing import BillingService
        service = BillingService()
        service.settings = mock_settings_dev
        
        # Any tenant should be allowed in dev mode
        assert await service.is_tenant_allowed("random-tenant-123") is True
        assert await service.is_tenant_allowed("another-tenant") is True
        assert await service.is_tenant_allowed("") is True  # Even empty
    
    @pytest.mark.asyncio
    async def test_prod_mode_allows_whitelisted_tenants(self, mock_settings_prod_with_whitelist, monkeypatch):
        """Production mode should only allow whitelisted tenants."""
        monkeypatch.setattr("app.services.billing.get_settings", lambda: mock_settings_prod_with_whitelist)
        
        from app.services.billing import BillingService
        service = BillingService()
        service.settings = mock_settings_prod_with_whitelist
        
        # Whitelisted tenants should be allowed
        assert await service.is_tenant_allowed("tenant-abc") is True
        assert await service.is_tenant_allowed("tenant-xyz") is True
        
        # Non-whitelisted tenants should be denied
        assert await service.is_tenant_allowed("tenant-other") is False
        assert await service.is_tenant_allowed("random") is False
    
    @pytest.mark.asyncio
    async def test_prod_mode_empty_whitelist_denies_all(self, mock_settings_prod_empty_whitelist, monkeypatch):
        """Production mode with empty whitelist should deny all (fail-closed security)."""
        monkeypatch.setattr("app.services.billing.get_settings", lambda: mock_settings_prod_empty_whitelist)
        
        from app.services.billing import BillingService
        service = BillingService()
        service.settings = mock_settings_prod_empty_whitelist
        
        # All tenants should be denied when whitelist is empty
        assert await service.is_tenant_allowed("tenant-abc") is False
        assert await service.is_tenant_allowed("any-tenant") is False
    
    @pytest.mark.asyncio
    async def test_whitelist_parsing_handles_whitespace(self, monkeypatch):
        """Whitelist parsing should handle extra whitespace."""
        settings = MagicMock()
        settings.app_env = "production"
        settings.allowed_tenants = " tenant-a , tenant-b , tenant-c "  # Extra spaces
        settings.enable_usage_tracking = True
        settings.database_url = None
        
        monkeypatch.setattr("app.services.billing.get_settings", lambda: settings)
        
        from app.services.billing import BillingService
        service = BillingService()
        service.settings = settings
        
        assert await service.is_tenant_allowed("tenant-a") is True
        assert await service.is_tenant_allowed("tenant-b") is True
        assert await service.is_tenant_allowed("tenant-c") is True
