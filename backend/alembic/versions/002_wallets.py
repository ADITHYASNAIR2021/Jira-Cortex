"""Add wallet tables for Financial Fortress

Revision ID: 002_wallets
Revises: 001_initial
Create Date: 2026-02-02 00:00:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '002_wallets'
down_revision = '001_initial'
branch_labels = None
depends_on = None


def upgrade() -> None:
    # TenantWallet table
    op.create_table(
        'tenant_wallets',
        sa.Column('tenant_id', sa.String(255), primary_key=True),
        sa.Column('balance', sa.Float(), nullable=False, server_default='0.0'),
        sa.Column('currency', sa.String(3), server_default='USD'),
        sa.Column('is_active', sa.Boolean(), server_default='false'),
        sa.Column('subscription_id', sa.String(255), nullable=True),
        sa.Column('subscription_end', sa.DateTime(), nullable=True),
        sa.Column('auto_recharge', sa.Boolean(), server_default='false'),
        sa.Column('auto_recharge_amount', sa.Float(), server_default='100.0'),
        sa.Column('auto_recharge_threshold', sa.Float(), server_default='10.0'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()')),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()')),
    )
    
    # PaymentTransaction table
    op.create_table(
        'payment_transactions',
        sa.Column('id', sa.String(255), primary_key=True),
        sa.Column('tenant_id', sa.String(255), sa.ForeignKey('tenant_wallets.tenant_id'), nullable=False),
        sa.Column('amount', sa.Float(), nullable=False),
        sa.Column('type', sa.String(50), nullable=False),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('status', sa.String(50), server_default='pending'),
        sa.Column('stripe_session_id', sa.String(255), nullable=True),
        sa.Column('balance_before', sa.Float(), nullable=True),
        sa.Column('balance_after', sa.Float(), nullable=True),
        sa.Column('created_at', sa.DateTime(), server_default=sa.text('now()')),
    )
    op.create_index('ix_payment_transactions_tenant_id', 'payment_transactions', ['tenant_id'])
    
    # UsageCost table (pricing configuration)
    op.create_table(
        'usage_costs',
        sa.Column('id', sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column('operation', sa.String(100), unique=True, nullable=False),
        sa.Column('cost_per_1k_tokens', sa.Float(), server_default='0.0'),
        sa.Column('cost_per_request', sa.Float(), server_default='0.0'),
        sa.Column('description', sa.String(500), nullable=True),
        sa.Column('is_active', sa.Boolean(), server_default='true'),
        sa.Column('updated_at', sa.DateTime(), server_default=sa.text('now()')),
    )
    
    # Insert default pricing
    op.execute("""
        INSERT INTO usage_costs (operation, cost_per_1k_tokens, cost_per_request, description) VALUES
        ('embedding', 0.00002, 0.0, 'text-embedding-3-small: $0.02 per 1M tokens'),
        ('query_input', 0.00015, 0.0, 'gpt-4o-mini input: $0.15 per 1M tokens'),
        ('query_output', 0.0006, 0.0, 'gpt-4o-mini output: $0.60 per 1M tokens'),
        ('cached_query', 0.0, 0.001, 'Cached query: $0.001 per request')
    """)


def downgrade() -> None:
    op.drop_table('usage_costs')
    op.drop_index('ix_payment_transactions_tenant_id', table_name='payment_transactions')
    op.drop_table('payment_transactions')
    op.drop_table('tenant_wallets')
