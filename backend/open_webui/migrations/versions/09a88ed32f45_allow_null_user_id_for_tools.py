"""Allow null user_id for tools

Revision ID: 09a88ed32f45
Revises: d31026856c01
Create Date: 2025-07-27 15:42:37.169903

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
import open_webui.internal.db
from sqlalchemy.dialects import sqlite

# revision identifiers, used by Alembic.
revision: str = '09a88ed32f45'
down_revision: Union[str, None] = 'd31026856c01'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

def upgrade() -> None:
    # Use batch mode for SQLite compatibility
    with op.batch_alter_table('tool', schema=None) as batch_op:
        batch_op.alter_column('user_id',
               existing_type=sa.String(),
               nullable=True)

def downgrade() -> None:
    # The downgrade operation also needs to be in batch mode
    with op.batch_alter_table('tool', schema=None) as batch_op:
        batch_op.alter_column('user_id',
               existing_type=sa.String(),
               nullable=False)
