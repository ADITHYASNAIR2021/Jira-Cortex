"""
Jira Cortex - Wallet Models

Pre-paid wallet system for tenant billing.
Part of the "Financial Fortress" architecture.
"""

from sqlalchemy import Column, String, Boolean, DateTime, Integer, ForeignKey, Numeric
from sqlalchemy.orm import relationship
from datetime import datetime, timezone

from app.services.billing import Base

def utc_now():
    return datetime.now(timezone.utc)


class TenantWallet(Base):
    """
    Tenant's pre-paid credit balance.
    
    The wallet holds real money (USD) that gets deducted
    as they use AI features (queries, embeddings, sync).
    """
    __tablename__ = "tenant_wallets"
    
    tenant_id = Column(String(255), primary_key=True)
    balance = Column(Numeric(10, 4), default=0.0, nullable=False)  # Real money ($)
    currency = Column(String(3), default="USD")
    
    # Subscription status (Platform Fee)
    is_active = Column(Boolean, default=False)  # True if $299/mo Platform Fee paid
    subscription_id = Column(String(255), nullable=True)  # Stripe Subscription ID
    subscription_end = Column(DateTime, nullable=True)  # When subscription expires
    
    # Auto-recharge settings
    auto_recharge = Column(Boolean, default=False)
    auto_recharge_amount = Column(Numeric(10, 4), default=100.0)  # Amount to charge when low
    auto_recharge_threshold = Column(Numeric(10, 4), default=10.0)  # Trigger when below this
    
    # Timestamps
    created_at = Column(DateTime, default=utc_now)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)
    
    # Relationship to transactions
    transactions = relationship("PaymentTransaction", back_populates="wallet")
    
    def __repr__(self):
        return f"<TenantWallet {self.tenant_id}: ${self.balance:.2f}>"


class PaymentTransaction(Base):
    """
    Record of all payment transactions.
    
    Tracks both:
    - Credits added (from Stripe payments)
    - Credits deducted (from AI usage)
    """
    __tablename__ = "payment_transactions"
    
    id = Column(String(255), primary_key=True)  # Stripe Session/Event ID or UUID
    tenant_id = Column(String(255), ForeignKey("tenant_wallets.tenant_id"), index=True)
    
    # Transaction details
    amount = Column(Numeric(10, 4), nullable=False)  # Positive = credit added, Negative = deducted
    type = Column(String(50), nullable=False)  # 'stripe_payment', 'usage_deduction', 'refund'
    description = Column(String(500), nullable=True)
    
    # Status tracking
    status = Column(String(50), default="pending")  # 'pending', 'succeeded', 'failed'
    stripe_session_id = Column(String(255), nullable=True)
    
    # Balance snapshot for audit trail
    balance_before = Column(Numeric(10, 4), nullable=True)
    balance_after = Column(Numeric(10, 4), nullable=True)
    
    created_at = Column(DateTime, default=utc_now)
    
    # Relationship
    wallet = relationship("TenantWallet", back_populates="transactions")
    
    def __repr__(self):
        return f"<Transaction {self.id}: ${self.amount:+.2f} ({self.type})>"


class UsageCost(Base):
    """
    Pricing configuration for different operations.
    
    Allows dynamic pricing without code changes.
    """
    __tablename__ = "usage_costs"
    
    id = Column(Integer, primary_key=True, autoincrement=True)
    operation = Column(String(100), unique=True, nullable=False)
    
    # Cost per unit (in USD)
    cost_per_1k_tokens = Column(Numeric(10, 4), default=0.0)  # For token-based ops
    cost_per_request = Column(Numeric(10, 4), default=0.0)   # For request-based ops
    
    # Metadata
    description = Column(String(500), nullable=True)
    is_active = Column(Boolean, default=True)
    updated_at = Column(DateTime, default=utc_now, onupdate=utc_now)
    
    def __repr__(self):
        return f"<UsageCost {self.operation}: ${self.cost_per_1k_tokens}/1k tokens>"
