"""Add frequency and value type to instruments.

Revision ID: 0002_instrument_semantics
Revises: 0001_initial
Create Date: 2026-11-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_instrument_semantics"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "instruments",
        sa.Column("frequency", sa.String(length=10), nullable=False, server_default="daily"),
    )
    op.add_column(
        "instruments",
        sa.Column("value_type", sa.String(length=20), nullable=False, server_default="price"),
    )


def downgrade() -> None:
    with op.batch_alter_table("instruments") as batch:
        batch.drop_column("value_type")
        batch.drop_column("frequency")
