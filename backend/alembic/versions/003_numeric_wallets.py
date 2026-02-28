"""Update wallet amounts from Float to Numeric(10, 4)

Revision ID: 003_numeric_wallets
Revises: 002_wallets
Create Date: 2026-03-01 02:26:00.000000

"""
from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = '003_numeric_wallets'
down_revision = '002_wallets'
branch_labels = None
depends_on = None

def upgrade() -> None:
    # Postgres handles Float -> Numeric casts inherently with USING ::numeric
    # TenantWallets
    op.execute("ALTER TABLE tenant_wallets ALTER COLUMN balance TYPE NUMERIC(10, 4) USING balance::numeric")
    op.execute("ALTER TABLE tenant_wallets ALTER COLUMN auto_recharge_amount TYPE NUMERIC(10, 4) USING auto_recharge_amount::numeric")
    op.execute("ALTER TABLE tenant_wallets ALTER COLUMN auto_recharge_threshold TYPE NUMERIC(10, 4) USING auto_recharge_threshold::numeric")

    # PaymentTransactions
    op.execute("ALTER TABLE payment_transactions ALTER COLUMN amount TYPE NUMERIC(10, 4) USING amount::numeric")
    op.execute("ALTER TABLE payment_transactions ALTER COLUMN balance_before TYPE NUMERIC(10, 4) USING balance_before::numeric")
    op.execute("ALTER TABLE payment_transactions ALTER COLUMN balance_after TYPE NUMERIC(10, 4) USING balance_after::numeric")

    # UsageCosts
    op.execute("ALTER TABLE usage_costs ALTER COLUMN cost_per_1k_tokens TYPE NUMERIC(10, 4) USING cost_per_1k_tokens::numeric")
    op.execute("ALTER TABLE usage_costs ALTER COLUMN cost_per_request TYPE NUMERIC(10, 4) USING cost_per_request::numeric")

def downgrade() -> None:
    # TenantWallets
    op.execute("ALTER TABLE tenant_wallets ALTER COLUMN balance TYPE DOUBLE PRECISION USING balance::double precision")
    op.execute("ALTER TABLE tenant_wallets ALTER COLUMN auto_recharge_amount TYPE DOUBLE PRECISION USING auto_recharge_amount::double precision")
    op.execute("ALTER TABLE tenant_wallets ALTER COLUMN auto_recharge_threshold TYPE DOUBLE PRECISION USING auto_recharge_threshold::double precision")

    # PaymentTransactions
    op.execute("ALTER TABLE payment_transactions ALTER COLUMN amount TYPE DOUBLE PRECISION USING amount::double precision")
    op.execute("ALTER TABLE payment_transactions ALTER COLUMN balance_before TYPE DOUBLE PRECISION USING balance_before::double precision")
    op.execute("ALTER TABLE payment_transactions ALTER COLUMN balance_after TYPE DOUBLE PRECISION USING balance_after::double precision")

    # UsageCosts
    op.execute("ALTER TABLE usage_costs ALTER COLUMN cost_per_1k_tokens TYPE DOUBLE PRECISION USING cost_per_1k_tokens::double precision")
    op.execute("ALTER TABLE usage_costs ALTER COLUMN cost_per_request TYPE DOUBLE PRECISION USING cost_per_request::double precision")
