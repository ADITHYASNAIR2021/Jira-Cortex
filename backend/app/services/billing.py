"""
Jira Cortex - Billing/Usage Service

Persistent usage tracking with SQLAlchemy.
Saves token consumption to PostgreSQL for billing.
"""

from datetime import datetime, date
from typing import Optional, List
from contextlib import asynccontextmanager
import structlog
from sqlalchemy import (
    Column, String, Integer, DateTime, Boolean, Date, 
    create_engine, Index, func
)
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.future import select

from app.config import get_settings

logger = structlog.get_logger(__name__)

Base = declarative_base()


class UsageRecord(Base):
    """
    Persistent usage record for billing.
    
    Stores token consumption per operation for monthly billing.
    """
    __tablename__ = "usage_records"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(255), nullable=False, index=True)
    user_account_id = Column(String(255), nullable=False)
    timestamp = Column(DateTime, default=datetime.utcnow, nullable=False)
    date = Column(Date, default=date.today, nullable=False, index=True)
    operation = Column(String(50), nullable=False)  # 'query' or 'ingest'
    input_tokens = Column(Integer, default=0, nullable=False)
    output_tokens = Column(Integer, default=0, nullable=False)
    total_tokens = Column(Integer, default=0, nullable=False)
    model = Column(String(100), nullable=False)
    cached = Column(Boolean, default=False, nullable=False)
    
    # Composite index for billing queries
    __table_args__ = (
        Index('ix_usage_tenant_date', 'tenant_id', 'date'),
    )


class MonthlyUsageSummary(Base):
    """
    Aggregated monthly usage for quick billing lookups.
    """
    __tablename__ = "monthly_usage_summary"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    tenant_id = Column(String(255), nullable=False, index=True)
    year = Column(Integer, nullable=False)
    month = Column(Integer, nullable=False)
    total_queries = Column(Integer, default=0, nullable=False)
    total_ingestions = Column(Integer, default=0, nullable=False)
    total_tokens = Column(Integer, default=0, nullable=False)
    total_cached_queries = Column(Integer, default=0, nullable=False)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    
    __table_args__ = (
        Index('ix_monthly_tenant_period', 'tenant_id', 'year', 'month', unique=True),
    )


class BillingService:
    """
    Service for tracking and billing token usage.
    
    Saves all API usage to PostgreSQL for:
    - Per-tenant monthly billing
    - Usage analytics
    - Cost tracking
    """
    
    def __init__(self):
        self.settings = get_settings()
        self._engine = None
        self._session_factory = None
    
    async def initialize(self) -> None:
        """
        Initialize database connection and create tables.
        
        Call this on app startup.
        """
        if not self.settings.enable_usage_tracking or not self.settings.database_url:
            logger.info("usage_tracking_disabled")
            return
        
        # Convert sync URL to async (postgresql:// -> postgresql+asyncpg://)
        db_url = self.settings.database_url
        if db_url.startswith("postgresql://"):
            db_url = db_url.replace("postgresql://", "postgresql+asyncpg://", 1)
        
        try:
            self._engine = create_async_engine(
                db_url,
                echo=self.settings.app_debug,
                pool_size=5,
                max_overflow=10
            )
            
            self._session_factory = sessionmaker(
                self._engine,
                class_=AsyncSession,
                expire_on_commit=False
            )
            
            # NOTE: We do NOT use Base.metadata.create_all() here.
            # Tables must be created via 'alembic upgrade head' in deployment.
            # Using create_all would bypass migrations and break schema updates.
            
            logger.info("billing_db_initialized")
            
        except Exception as e:
            logger.error("billing_db_init_failed", error=str(e))
            # Don't raise - billing failure shouldn't break the app
    
    @asynccontextmanager
    async def _get_session(self):
        """Get async database session."""
        if not self._session_factory:
            yield None
            return
            
        async with self._session_factory() as session:
            try:
                yield session
                await session.commit()
            except Exception:
                await session.rollback()
                raise
    
    async def record_usage(
        self,
        tenant_id: str,
        user_account_id: str,
        operation: str,
        input_tokens: int,
        output_tokens: int,
        model: str,
        cached: bool = False
    ) -> bool:
        """
        Record a usage event to the database.
        
        Args:
            tenant_id: Atlassian tenant/cloud ID
            user_account_id: User who made the request
            operation: 'query' or 'ingest'
            input_tokens: Input tokens consumed
            output_tokens: Output tokens consumed
            model: Model used (e.g., 'gpt-4o')
            cached: Whether response was cached
            
        Returns:
            True if recorded successfully
        """
        if not self.settings.enable_usage_tracking:
            return True
        
        total_tokens = input_tokens + output_tokens
        
        try:
            async with self._get_session() as session:
                if session is None:
                    # Database not configured, just log
                    logger.info(
                        "usage_logged_no_db",
                        tenant_id=tenant_id,
                        operation=operation,
                        tokens=total_tokens
                    )
                    return True
                
                # Create usage record
                record = UsageRecord(
                    tenant_id=tenant_id,
                    user_account_id=user_account_id,
                    operation=operation,
                    input_tokens=input_tokens,
                    output_tokens=output_tokens,
                    total_tokens=total_tokens,
                    model=model,
                    cached=cached
                )
                
                session.add(record)
                
                logger.debug(
                    "usage_recorded",
                    tenant_id=tenant_id,
                    operation=operation,
                    tokens=total_tokens
                )
                
                return True
                
        except Exception as e:
            logger.error("usage_record_failed", error=str(e))
            # Don't raise - usage tracking failure shouldn't break queries
            return False
    
    async def get_tenant_usage(
        self,
        tenant_id: str,
        start_date: date,
        end_date: date
    ) -> dict:
        """
        Get usage summary for a tenant in a date range.
        
        Args:
            tenant_id: Tenant to query
            start_date: Start of billing period
            end_date: End of billing period
            
        Returns:
            Usage summary dict
        """
        async with self._get_session() as session:
            if session is None:
                return {"error": "Database not configured"}
            
            # Query aggregated usage
            result = await session.execute(
                select(
                    func.count(UsageRecord.id).label('total_requests'),
                    func.sum(UsageRecord.total_tokens).label('total_tokens'),
                    func.sum(UsageRecord.input_tokens).label('input_tokens'),
                    func.sum(UsageRecord.output_tokens).label('output_tokens')
                )
                .where(UsageRecord.tenant_id == tenant_id)
                .where(UsageRecord.date >= start_date)
                .where(UsageRecord.date <= end_date)
            )
            
            row = result.first()
            
            return {
                "tenant_id": tenant_id,
                "period": {
                    "start": start_date.isoformat(),
                    "end": end_date.isoformat()
                },
                "usage": {
                    "total_requests": row.total_requests or 0,
                    "total_tokens": row.total_tokens or 0,
                    "input_tokens": row.input_tokens or 0,
                    "output_tokens": row.output_tokens or 0
                }
            }
    
    async def get_monthly_bill(self, tenant_id: str, year: int, month: int) -> dict:
        """
        Calculate monthly bill for a tenant.
        
        Uses standard pricing:
        - $0.01 per 1K input tokens
        - $0.03 per 1K output tokens
        """
        from datetime import date
        import calendar
        
        start_date = date(year, month, 1)
        _, last_day = calendar.monthrange(year, month)
        end_date = date(year, month, last_day)
        
        usage = await self.get_tenant_usage(tenant_id, start_date, end_date)
        
        if "error" in usage:
            return usage
        
        # Calculate cost (example pricing)
        input_cost = (usage["usage"]["input_tokens"] / 1000) * 0.01
        output_cost = (usage["usage"]["output_tokens"] / 1000) * 0.03
        total_cost = input_cost + output_cost
        
        return {
            **usage,
            "billing": {
                "input_cost_usd": round(input_cost, 4),
                "output_cost_usd": round(output_cost, 4),
                "total_cost_usd": round(total_cost, 2)
            }
        }
    
    async def is_tenant_allowed(self, tenant_id: str) -> bool:
        """
        Check if tenant is allowed to use the service.
        
        Priority order:
        1. Development mode: allow all tenants
        2. Active wallet subscription (is_active=True): allow
        3. Whitelist fallback (ALLOWED_TENANTS): allow beta users/admins
        
        CRITICAL: This prevents "free lunch" abuse where unsubscribed
        tenants consume OpenAI tokens without paying.
        
        Args:
            tenant_id: Atlassian cloud ID
            
        Returns:
            True if tenant can use the service
        """
        # 1. Development mode: allow all tenants for testing
        if self.settings.app_env == "development":
            logger.debug("tenant_allowed_dev_mode", tenant_id=tenant_id)
            return True
        
        # 2. Check Wallet / Subscription Status (Primary method)
        wallet = await self.get_wallet(tenant_id)
        if wallet and wallet.is_active:
            logger.debug("tenant_allowed_subscription", tenant_id=tenant_id)
            return True
        
        # 3. Fallback to Whitelist (for beta users/admins who don't pay through Stripe)
        allowed = [t.strip() for t in self.settings.allowed_tenants.split(",") if t.strip()]
        
        if tenant_id in allowed:
            logger.debug("tenant_allowed_whitelist", tenant_id=tenant_id)
            return True
        
        # Not allowed - log and deny
        logger.warning(
            "tenant_not_allowed",
            tenant_id=tenant_id,
            has_wallet=wallet is not None,
            wallet_active=wallet.is_active if wallet else False
        )
        return False
    
    # -----------------------------------------
    # Wallet Methods (Financial Fortress)
    # -----------------------------------------
    
    async def get_wallet(self, tenant_id: str):
        """Get tenant's wallet."""
        from app.models.wallet import TenantWallet
        
        async with self._get_session() as session:
            if session is None:
                return None
            return await session.get(TenantWallet, tenant_id)
    
    async def create_wallet(self, tenant_id: str):
        """Create wallet for new tenant."""
        from app.models.wallet import TenantWallet
        
        async with self._get_session() as session:
            if session is None:
                return None
            
            wallet = TenantWallet(
                tenant_id=tenant_id,
                balance=0.0,
                is_active=False
            )
            session.add(wallet)
            return wallet
    
    async def get_balance(self, tenant_id: str) -> float:
        """Get current wallet balance."""
        wallet = await self.get_wallet(tenant_id)
        return wallet.balance if wallet else 0.0
    
    async def has_sufficient_funds(self, tenant_id: str, estimated_cost: float) -> bool:
        """
        Gatekeeper: Check if tenant can afford this action.
        
        CRITICAL: This is the "No Cash, No Query" gate.
        """
        # Development mode: allow all
        if self.settings.app_env == "development":
            return True
        
        balance = await self.get_balance(tenant_id)
        if balance < estimated_cost:
            logger.warning(
                "insufficient_funds",
                tenant_id=tenant_id,
                balance=balance,
                required=estimated_cost
            )
            return False
        return True
    
    async def deduct_balance(self, tenant_id: str, cost: float, description: str = "") -> bool:
        """
        The Money Extractor.
        
        Deducts cost from wallet atomically.
        Returns False if wallet doesn't exist.
        """
        from app.models.wallet import TenantWallet, PaymentTransaction
        import uuid
        
        if self.settings.app_env == "development":
            return True
        
        async with self._get_session() as session:
            if session is None:
                return False
            
            wallet = await session.get(TenantWallet, tenant_id)
            if not wallet:
                return False
            
            balance_before = wallet.balance
            wallet.balance -= cost
            
            # Record transaction
            transaction = PaymentTransaction(
                id=str(uuid.uuid4()),
                tenant_id=tenant_id,
                amount=-cost,  # Negative for deduction
                type="usage_deduction",
                description=description or "AI processing usage",
                status="succeeded",
                balance_before=balance_before,
                balance_after=wallet.balance
            )
            
            session.add(wallet)
            session.add(transaction)
            
            logger.info(
                "balance_deducted",
                tenant_id=tenant_id,
                cost=cost,
                new_balance=wallet.balance
            )
            
            return True
    
    async def add_credits(
        self,
        tenant_id: str,
        amount: float,
        stripe_session_id: str = None
    ) -> bool:
        """
        Add credits to wallet (from Stripe payment).
        """
        from app.models.wallet import TenantWallet, PaymentTransaction
        import uuid
        
        async with self._get_session() as session:
            if session is None:
                return False
            
            wallet = await session.get(TenantWallet, tenant_id)
            if not wallet:
                # Create wallet if doesn't exist
                wallet = TenantWallet(tenant_id=tenant_id, balance=0.0)
                session.add(wallet)
            
            balance_before = wallet.balance
            wallet.balance += amount
            
            # Record transaction
            transaction = PaymentTransaction(
                id=stripe_session_id or str(uuid.uuid4()),
                tenant_id=tenant_id,
                amount=amount,
                type="stripe_payment",
                description=f"Added ${amount:.2f} in credits",
                status="succeeded",
                stripe_session_id=stripe_session_id,
                balance_before=balance_before,
                balance_after=wallet.balance
            )
            
            session.add(transaction)
            
            logger.info(
                "credits_added",
                tenant_id=tenant_id,
                amount=amount,
                new_balance=wallet.balance
            )
            
            return True
    
    async def activate_subscription(self, tenant_id: str, subscription_id: str) -> bool:
        """Activate platform subscription."""
        from app.models.wallet import TenantWallet
        from datetime import timedelta
        
        async with self._get_session() as session:
            if session is None:
                return False
            
            wallet = await session.get(TenantWallet, tenant_id)
            if not wallet:
                wallet = TenantWallet(tenant_id=tenant_id)
                session.add(wallet)
            
            wallet.is_active = True
            wallet.subscription_id = subscription_id
            wallet.subscription_end = datetime.utcnow() + timedelta(days=30)
            
            logger.info("subscription_activated", tenant_id=tenant_id)
            return True
    
    async def deactivate_subscription(self, subscription_id: str) -> bool:
        """Deactivate subscription by Stripe subscription ID."""
        from app.models.wallet import TenantWallet
        
        async with self._get_session() as session:
            if session is None:
                return False
            
            result = await session.execute(
                select(TenantWallet).where(TenantWallet.subscription_id == subscription_id)
            )
            wallet = result.scalar_one_or_none()
            
            if wallet:
                wallet.is_active = False
                logger.warning("subscription_deactivated", tenant_id=wallet.tenant_id)
                return True
            return False
    
    async def extend_subscription(self, subscription_id: str) -> bool:
        """Extend subscription by 30 days (on renewal)."""
        from app.models.wallet import TenantWallet
        from datetime import timedelta
        
        async with self._get_session() as session:
            if session is None:
                return False
            
            result = await session.execute(
                select(TenantWallet).where(TenantWallet.subscription_id == subscription_id)
            )
            wallet = result.scalar_one_or_none()
            
            if wallet:
                wallet.subscription_end = datetime.utcnow() + timedelta(days=30)
                return True
            return False
    
    async def get_transactions(self, tenant_id: str, limit: int = 50):
        """Get recent transactions for a tenant."""
        from app.models.wallet import PaymentTransaction
        
        async with self._get_session() as session:
            if session is None:
                return []
            
            result = await session.execute(
                select(PaymentTransaction)
                .where(PaymentTransaction.tenant_id == tenant_id)
                .order_by(PaymentTransaction.created_at.desc())
                .limit(limit)
            )
            return result.scalars().all()
    
    def calculate_embedding_cost(self, token_count: int) -> float:
        """Calculate cost for embedding tokens (text-embedding-3-small)."""
        # OpenAI pricing: $0.02 per 1M tokens for text-embedding-3-small
        cost_per_million = 0.02
        return (token_count / 1_000_000) * cost_per_million
    
    def calculate_query_cost(self, input_tokens: int, output_tokens: int) -> float:
        """Calculate cost for LLM query (gpt-4o-mini)."""
        # OpenAI pricing: $0.15/1M input, $0.60/1M output for gpt-4o-mini
        input_cost = (input_tokens / 1_000_000) * 0.15
        output_cost = (output_tokens / 1_000_000) * 0.60
        return input_cost + output_cost
    
    async def close(self) -> None:
        """Close database connections."""
        if self._engine:
            await self._engine.dispose()


# Singleton instance
_billing_service: Optional[BillingService] = None


def get_billing_service() -> BillingService:
    """Get or create billing service singleton."""
    global _billing_service
    if _billing_service is None:
        _billing_service = BillingService()
    return _billing_service
