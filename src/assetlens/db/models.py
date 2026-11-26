"""SQLAlchemy models.

The schema is intentionally small and provider-agnostic:

* ``instruments`` - one row per tracked instrument (fund, ETF, stock, ...).
* ``prices`` - one row per instrument and trading date (idempotent upsert key).
* ``ingestion_runs`` - audit trail of every sync, useful for debugging and
  for detecting stale data.
"""

from __future__ import annotations

from datetime import UTC, date, datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import TypeDecorator


def utcnow() -> datetime:
    """Timezone-aware UTC timestamp used as column default."""

    return datetime.now(UTC)


class UTCDateTime(TypeDecorator[datetime]):
    """Persist UTC timestamps across SQLite and PostgreSQL.

    SQLite has no native timezone support, so values are stored as naive UTC
    and re-attached on read. PostgreSQL uses ``TIMESTAMPTZ`` and keeps the
    offset natively. Either way every timestamp is unambiguous.
    """

    impl = DateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            msg = "naive datetimes cannot be stored; pass a timezone-aware value"
            raise ValueError(msg)
        normalized = value.astimezone(UTC)
        return normalized.replace(tzinfo=None) if dialect.name == "sqlite" else normalized

    def process_result_value(self, value: datetime | None, _dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)


class Base(DeclarativeBase):
    """Declarative base for every model."""


class Instrument(Base):
    __tablename__ = "instruments"
    __table_args__ = (
        UniqueConstraint("provider", "symbol", name="uq_instruments_provider_symbol"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    symbol: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    asset_class: Mapped[str] = mapped_column(String(20), nullable=False, default="fund")
    frequency: Mapped[str] = mapped_column(
        String(10), nullable=False, default="daily", server_default="daily"
    )
    value_type: Mapped[str] = mapped_column(
        String(20), nullable=False, default="price", server_default="price"
    )
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    isin: Mapped[str | None] = mapped_column(String(12), nullable=True)
    url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    meta: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, nullable=False, default=dict)
    active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utcnow, onupdate=utcnow
    )

    prices: Mapped[list[Price]] = relationship(
        back_populates="instrument", cascade="all, delete-orphan", passive_deletes=True
    )
    runs: Mapped[list[IngestionRun]] = relationship(back_populates="instrument")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"Instrument(id={self.id!r}, provider={self.provider!r}, symbol={self.symbol!r})"


class Price(Base):
    __tablename__ = "prices"
    __table_args__ = (Index("ix_prices_price_date", "price_date"),)

    instrument_id: Mapped[int] = mapped_column(
        ForeignKey("instruments.id", ondelete="CASCADE"), primary_key=True
    )
    price_date: Mapped[date] = mapped_column(Date, primary_key=True)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    source: Mapped[str] = mapped_column(String(50), nullable=False)
    ingested_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)

    instrument: Mapped[Instrument] = relationship(back_populates="prices")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"Price(instrument_id={self.instrument_id!r}, "
            f"date={self.price_date!r}, value={self.value!r})"
        )


class IngestionRun(Base):
    __tablename__ = "ingestion_runs"
    __table_args__ = (Index("ix_ingestion_runs_started_at", "started_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    instrument_id: Mapped[int | None] = mapped_column(
        ForeignKey("instruments.id", ondelete="SET NULL"), nullable=True
    )
    symbol: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="running")
    requested_from: Mapped[date | None] = mapped_column(Date, nullable=True)
    requested_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    rows_fetched: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_inserted: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    rows_updated: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(UTCDateTime(), nullable=True)

    instrument: Mapped[Instrument | None] = relationship(back_populates="runs")

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"IngestionRun(id={self.id!r}, symbol={self.symbol!r}, status={self.status!r})"


class PolicySnapshot(Base):
    """One self-contained valuation of a life-insurance policy.

    The policy is intentionally stored in a single table: the premium schedule
    and the holdings are JSON documents, while every figure needed for SQL
    analysis (gross value, tax, net value, TIR) is a first-class column.
    """

    __tablename__ = "policy_snapshots"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    as_of: Mapped[date] = mapped_column(Date, nullable=False, unique=True)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="EUR")
    total_premiums: Mapped[float] = mapped_column(Float, nullable=False)
    gesav_value: Mapped[float] = mapped_column(Float, nullable=False)
    funds_value: Mapped[float] = mapped_column(Float, nullable=False)
    gross_value: Mapped[float] = mapped_column(Float, nullable=False)
    capital_gain: Mapped[float] = mapped_column(Float, nullable=False)
    tax_rate: Mapped[float] = mapped_column(Float, nullable=False)
    tax_amount: Mapped[float] = mapped_column(Float, nullable=False)
    net_value: Mapped[float] = mapped_column(Float, nullable=False)
    gross_irr_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    net_irr_pct: Mapped[float | None] = mapped_column(Float, nullable=True)
    premiums: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    holdings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        UTCDateTime(), nullable=False, default=utcnow, onupdate=utcnow
    )

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"PolicySnapshot(as_of={self.as_of!r}, net_value={self.net_value!r})"
