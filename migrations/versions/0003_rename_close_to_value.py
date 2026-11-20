"""Rename prices.close to prices.value.

Observations are not always prices: separate accounts publish return
rates. The generic column name reflects that the meaning is declared by
``instruments.value_type``.

Revision ID: 0003_rename_close_to_value
Revises: 0002_instrument_semantics
Create Date: 2026-11-20
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "0003_rename_close_to_value"
down_revision: str | None = "0002_instrument_semantics"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("prices") as batch:
        batch.alter_column("close", new_column_name="value")


def downgrade() -> None:
    with op.batch_alter_table("prices") as batch:
        batch.alter_column("value", new_column_name="close")
