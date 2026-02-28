"""Initial billing tables

Revision ID: 001_initial
Revises:
Create Date: 2026-02-01

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = '001_initial'
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Create usage_records table for billing."""
    op.create_table(
        'usage_records',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('tenant_id', sa.String(255), nullable=False, index=True),
        sa.Column('user_account_id', sa.String(255), nullable=False),
        sa.Column('operation', sa.String(50), nullable=False),
        sa.Column('input_tokens', sa.Integer(), nullable=False, default=0),
        sa.Column('output_tokens', sa.Integer(), nullable=False, default=0),
        sa.Column('model', sa.String(100), nullable=False),
        sa.Column('cached', sa.Boolean(), nullable=False, default=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        
        # Indexes for billing queries
        sa.Index('ix_usage_tenant_date', 'tenant_id', 'created_at'),
        sa.Index('ix_usage_user_date', 'user_account_id', 'created_at'),
    )


def downgrade() -> None:
    """Drop usage_records table."""
    op.drop_table('usage_records')
