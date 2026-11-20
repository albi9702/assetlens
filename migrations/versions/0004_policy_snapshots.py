"""Add the policy_snapshots table.

A policy valuation is stored as one self-contained row: the premium schedule
and the holdings live in JSON columns, while the gross value, the tax, the net
value and the TIR are first-class columns for SQL analysis.

Revision ID: 0004_policy_snapshots
Revises: 0003_rename_close_to_value
Create Date: 2026-11-20
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004_policy_snapshots"
down_revision: str | None = "0003_rename_close_to_value"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "policy_snapshots",
        sa.Column("id", sa.Integer(), autoincrement=True, nullable=False),
        sa.Column("as_of", sa.Date(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("total_premiums", sa.Float(), nullable=False),
        sa.Column("gesav_value", sa.Float(), nullable=False),
        sa.Column("funds_value", sa.Float(), nullable=False),
        sa.Column("gross_value", sa.Float(), nullable=False),
        sa.Column("capital_gain", sa.Float(), nullable=False),
        sa.Column("tax_rate", sa.Float(), nullable=False),
        sa.Column("tax_amount", sa.Float(), nullable=False),
        sa.Column("net_value", sa.Float(), nullable=False),
        sa.Column("gross_irr_pct", sa.Float(), nullable=True),
        sa.Column("net_irr_pct", sa.Float(), nullable=True),
        sa.Column("premiums", sa.JSON(), nullable=False),
        sa.Column("holdings", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("as_of", name="uq_policy_snapshots_as_of"),
    )


def downgrade() -> None:
    op.drop_table("policy_snapshots")
