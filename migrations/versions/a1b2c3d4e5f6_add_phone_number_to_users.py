"""Add phone_number to users table

Revision ID: a1b2c3d4e5f6
Revises: f3ec7425f040
Create Date: 2026-10-07

Adds phone_number column to users table for supplier invitation
via contact sharing. Includes index for fast lookup.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b2c3d4e5f6'
down_revision: Union[str, None] = '4dfb7a7c3a1e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Add phone_number column (nullable, text type per project convention)
    op.add_column(
        'users',
        sa.Column('phone_number', sa.Text(), nullable=True,
                  comment='Normalized phone number for contact-based invitations')
    )

    # Create index for fast lookup by phone number
    op.create_index(
        'ix_users_phone_number',
        'users',
        ['phone_number'],
        unique=False
    )

    # Add a CHECK constraint: phone_number must be digits only when present
    # Normalized format: digits only, no +, -, spaces
    op.create_check_constraint(
        'ck_users_phone_number_format',
        'users',
        "phone_number IS NULL OR phone_number ~ '^[0-9]{7,15}$'"
    )


def downgrade() -> None:
    op.drop_constraint('ck_users_phone_number_format', 'users', type_='check')
    op.drop_index('ix_users_phone_number', table_name='users')
    op.drop_column('users', 'phone_number')