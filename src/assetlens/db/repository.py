"""Persistence layer.

All writes are idempotent: prices are keyed by ``(instrument_id, price_date)``
and upserted with ``ON CONFLICT DO UPDATE``. Reads return Polars DataFrames so
that analytics never needs pandas.
"""

from __future__ import annotations

import math
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import polars as pl
from sqlalchemy import Engine, func, select
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.orm import sessionmaker

from assetlens.config import InstrumentConfig
from assetlens.db.models import IngestionRun, Instrument, PolicySnapshot, Price, utcnow
from assetlens.errors import InstrumentNotFoundError, PolicySnapshotNotFoundError
from assetlens.services.policy import PolicySummary

MIN_RETURN_PCT = -100.0

OBSERVATION_COLUMNS = ["price_date", "value", "source", "ingested_at"]
OBSERVATION_SCHEMA: dict[str, Any] = {
    "price_date": pl.Date,
    "value": pl.Float64,
    "source": pl.String,
    "ingested_at": pl.Datetime("us", "UTC"),
}
INSTRUMENT_COLUMNS = [
    "id",
    "provider",
    "symbol",
    "name",
    "asset_class",
    "frequency",
    "value_type",
    "currency",
    "isin",
    "active",
    "first_date",
    "last_date",
    "observations",
    "last_value",
]
INSTRUMENT_SCHEMA: dict[str, Any] = {
    "id": pl.Int64,
    "provider": pl.String,
    "symbol": pl.String,
    "name": pl.String,
    "asset_class": pl.String,
    "frequency": pl.String,
    "value_type": pl.String,
    "currency": pl.String,
    "isin": pl.String,
    "active": pl.Boolean,
    "first_date": pl.Date,
    "last_date": pl.Date,
    "observations": pl.Int64,
    "last_value": pl.Float64,
}
RUN_COLUMNS = [
    "id",
    "provider",
    "symbol",
    "status",
    "requested_from",
    "requested_to",
    "rows_fetched",
    "rows_inserted",
    "rows_updated",
    "error",
    "started_at",
    "finished_at",
]
RUN_SCHEMA: dict[str, Any] = {
    "id": pl.Int64,
    "provider": pl.String,
    "symbol": pl.String,
    "status": pl.String,
    "requested_from": pl.Date,
    "requested_to": pl.Date,
    "rows_fetched": pl.Int64,
    "rows_inserted": pl.Int64,
    "rows_updated": pl.Int64,
    "error": pl.String,
    "started_at": pl.Datetime("us", "UTC"),
    "finished_at": pl.Datetime("us", "UTC"),
}
POLICY_COLUMNS = [
    "id",
    "as_of",
    "currency",
    "total_premiums",
    "gesav_value",
    "funds_value",
    "gross_value",
    "capital_gain",
    "tax_rate",
    "tax_amount",
    "net_value",
    "gross_irr_pct",
    "net_irr_pct",
    "created_at",
]
POLICY_SCHEMA: dict[str, Any] = {
    "id": pl.Int64,
    "as_of": pl.Date,
    "currency": pl.String,
    "total_premiums": pl.Float64,
    "gesav_value": pl.Float64,
    "funds_value": pl.Float64,
    "gross_value": pl.Float64,
    "capital_gain": pl.Float64,
    "tax_rate": pl.Float64,
    "tax_amount": pl.Float64,
    "net_value": pl.Float64,
    "gross_irr_pct": pl.Float64,
    "net_irr_pct": pl.Float64,
    "created_at": pl.Datetime("us", "UTC"),
}


@dataclass(frozen=True, slots=True)
class UpsertStats:
    inserted: int
    updated: int

    @property
    def total(self) -> int:
        return self.inserted + self.updated


def _rows_to_frame(
    rows: Sequence[Any], columns: Sequence[str], schema: dict[str, Any]
) -> pl.DataFrame:
    if not rows:
        return pl.DataFrame(schema=schema)
    data = {column: [row._mapping[column] for row in rows] for column in columns}
    return pl.DataFrame(data, schema=schema)


def _chunks(items: Sequence[dict[str, Any]], size: int) -> Iterator[Sequence[dict[str, Any]]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


class Repository:
    """High-level data access used by the ingestion service, CLI and API."""

    def __init__(self, engine: Engine) -> None:
        self.engine = engine
        self._session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    # ------------------------------------------------------------------ instruments

    def upsert_instrument(self, config: InstrumentConfig) -> int:
        """Insert or update an instrument from the watchlist and return its id."""

        with self._session_factory() as session, session.begin():
            instrument = session.scalar(
                select(Instrument).where(
                    Instrument.provider == config.provider,
                    Instrument.symbol == config.symbol,
                )
            )
            if instrument is None:
                instrument = Instrument(
                    provider=config.provider,
                    symbol=config.symbol,
                    name=config.name,
                    asset_class=config.asset_class,
                    frequency=config.frequency,
                    value_type=config.value_type,
                    currency=config.currency,
                    isin=config.isin,
                    url=config.url,
                    meta=config.metadata,
                    active=config.active,
                )
                session.add(instrument)
            else:
                instrument.name = config.name
                instrument.asset_class = config.asset_class
                instrument.frequency = config.frequency
                instrument.value_type = config.value_type
                instrument.currency = config.currency
                instrument.isin = config.isin
                instrument.url = config.url
                instrument.meta = config.metadata
                instrument.active = config.active
                instrument.updated_at = utcnow()
            session.flush()
            return instrument.id

    def get_instrument(self, symbol: str, provider: str | None = None) -> Instrument:
        with self._session_factory() as session:
            statement = select(Instrument).where(Instrument.symbol == symbol)
            if provider is not None:
                statement = statement.where(Instrument.provider == provider)
            instruments = session.scalars(statement).all()
        if not instruments:
            msg = f"instrument not found: {symbol}"
            raise InstrumentNotFoundError(msg)
        if len(instruments) > 1:
            msg = f"symbol {symbol!r} exists for multiple providers; specify one"
            raise InstrumentNotFoundError(msg)
        return instruments[0]

    def list_instruments(self, *, active_only: bool = False) -> pl.DataFrame:
        last_value = (
            select(Price.value)
            .where(Price.instrument_id == Instrument.id)
            .order_by(Price.price_date.desc())
            .limit(1)
            .correlate(Instrument)
            .scalar_subquery()
        )
        statement = (
            select(
                Instrument.id,
                Instrument.provider,
                Instrument.symbol,
                Instrument.name,
                Instrument.asset_class,
                Instrument.frequency,
                Instrument.value_type,
                Instrument.currency,
                Instrument.isin,
                Instrument.active,
                func.min(Price.price_date).label("first_date"),
                func.max(Price.price_date).label("last_date"),
                func.count(Price.price_date).label("observations"),
                last_value.label("last_value"),
            )
            .outerjoin(Price, Price.instrument_id == Instrument.id)
            .group_by(Instrument.id)
            .order_by(Instrument.name)
        )
        if active_only:
            statement = statement.where(Instrument.active.is_(True))
        with self._session_factory() as session:
            rows = session.execute(statement).all()
        return _rows_to_frame(rows, INSTRUMENT_COLUMNS, INSTRUMENT_SCHEMA)

    # ---------------------------------------------------------------------- prices

    def upsert_prices(
        self,
        instrument_id: int,
        frame: pl.DataFrame,
        *,
        source: str,
        value_type: str = "price",
    ) -> UpsertStats:
        """Upsert a normalized observation frame (``price_date`` and ``value``).

        ``value_type`` decides which values are valid: prices must be
        strictly positive, while return rates may be zero or negative but
        cannot be below -100%.
        """

        if value_type not in {"price", "return_pct"}:
            msg = f"unsupported value_type: {value_type!r}"
            raise ValueError(msg)
        if frame.is_empty():
            return UpsertStats(inserted=0, updated=0)

        missing = {"price_date", "value"} - set(frame.columns)
        if missing:
            msg = f"price frame is missing columns: {sorted(missing)}"
            raise ValueError(msg)

        records: list[dict[str, Any]] = []
        for row in frame.select(["price_date", "value"]).sort("price_date").iter_rows(named=True):
            value = float(row["value"])
            if not math.isfinite(value):
                msg = f"invalid value for {row['price_date']}: {row['value']!r}"
                raise ValueError(msg)
            if value_type == "price" and value <= 0:
                msg = f"prices must be positive, got {value!r} on {row['price_date']}"
                raise ValueError(msg)
            if value_type == "return_pct" and value <= MIN_RETURN_PCT:
                msg = f"return rates must be above -100%, got {value!r} on {row['price_date']}"
                raise ValueError(msg)
            records.append(
                {
                    "instrument_id": instrument_id,
                    "price_date": row["price_date"],
                    "value": value,
                    "source": source,
                    "ingested_at": utcnow(),
                }
            )

        dates = [record["price_date"] for record in records]
        if len(set(dates)) != len(dates):
            msg = "price frame contains duplicate dates"
            raise ValueError(msg)

        with self._session_factory() as session, session.begin():
            existing = set(
                session.scalars(
                    select(Price.price_date).where(
                        Price.instrument_id == instrument_id,
                        Price.price_date.in_(dates),
                    )
                ).all()
            )
            inserted = sum(1 for value in dates if value not in existing)
            updated = len(dates) - inserted

            if self.engine.dialect.name == "sqlite":
                for chunk in _chunks(records, 500):
                    statement = sqlite_insert(Price).values(list(chunk))
                    statement = statement.on_conflict_do_update(
                        index_elements=[Price.instrument_id, Price.price_date],
                        set_={
                            "value": statement.excluded.value,
                            "source": statement.excluded.source,
                            "ingested_at": statement.excluded.ingested_at,
                        },
                    )
                    session.execute(statement)
            else:  # pragma: no cover - SQLite is the only supported backend for now
                for record in records:
                    session.merge(Price(**record))

        return UpsertStats(inserted=inserted, updated=updated)

    def latest_price_date(self, instrument_id: int) -> date | None:
        with self._session_factory() as session:
            return session.scalar(
                select(func.max(Price.price_date)).where(Price.instrument_id == instrument_id)
            )

    def get_prices(
        self,
        symbol: str,
        *,
        provider: str | None = None,
        start: date | None = None,
        end: date | None = None,
        limit: int | None = None,
    ) -> pl.DataFrame:
        """Return stored prices for ``symbol`` ordered by date ascending."""

        instrument = self.get_instrument(symbol, provider)
        conditions = [Price.instrument_id == instrument.id]
        if start is not None:
            conditions.append(Price.price_date >= start)
        if end is not None:
            conditions.append(Price.price_date <= end)

        statement = select(Price.price_date, Price.value, Price.source, Price.ingested_at).where(
            *conditions
        )
        statement = (
            statement.order_by(Price.price_date.desc()).limit(limit)
            if limit is not None
            else statement.order_by(Price.price_date.asc())
        )
        with self._session_factory() as session:
            rows = session.execute(statement).all()
        frame = _rows_to_frame(rows, OBSERVATION_COLUMNS, OBSERVATION_SCHEMA)
        return frame.sort("price_date") if limit is not None else frame

    def count_prices(self, symbol: str | None = None, *, provider: str | None = None) -> int:
        statement = select(func.count(Price.price_date))
        if symbol is not None:
            instrument = self.get_instrument(symbol, provider)
            statement = statement.where(Price.instrument_id == instrument.id)
        with self._session_factory() as session:
            return int(session.scalar(statement) or 0)

    # ------------------------------------------------------------------ ingestion runs

    def start_run(
        self,
        *,
        provider: str,
        symbol: str,
        instrument_id: int | None,
        requested_from: date | None,
        requested_to: date | None,
    ) -> int:
        with self._session_factory() as session, session.begin():
            run = IngestionRun(
                provider=provider,
                symbol=symbol,
                instrument_id=instrument_id,
                status="running",
                requested_from=requested_from,
                requested_to=requested_to,
            )
            session.add(run)
            session.flush()
            return run.id

    def finish_run(
        self,
        run_id: int,
        *,
        status: str,
        rows_fetched: int = 0,
        rows_inserted: int = 0,
        rows_updated: int = 0,
        error: str | None = None,
    ) -> None:
        with self._session_factory() as session, session.begin():
            run = session.get(IngestionRun, run_id)
            if run is None:  # pragma: no cover - defensive
                return
            run.status = status
            run.rows_fetched = rows_fetched
            run.rows_inserted = rows_inserted
            run.rows_updated = rows_updated
            run.error = error
            run.finished_at = utcnow()

    def list_runs(self, limit: int = 20) -> pl.DataFrame:
        statement = (
            select(
                IngestionRun.id,
                IngestionRun.provider,
                IngestionRun.symbol,
                IngestionRun.status,
                IngestionRun.requested_from,
                IngestionRun.requested_to,
                IngestionRun.rows_fetched,
                IngestionRun.rows_inserted,
                IngestionRun.rows_updated,
                IngestionRun.error,
                IngestionRun.started_at,
                IngestionRun.finished_at,
            )
            .order_by(IngestionRun.started_at.desc())
            .limit(limit)
        )
        with self._session_factory() as session:
            rows = session.execute(statement).all()
        return _rows_to_frame(rows, RUN_COLUMNS, RUN_SCHEMA)

    # ------------------------------------------------------------- policy snapshots

    def upsert_policy_snapshot(self, summary: PolicySummary) -> int:
        """Insert or update the one-row snapshot for ``summary.as_of``."""

        values = {
            "currency": summary.currency,
            "total_premiums": summary.total_premiums,
            "gesav_value": summary.gesav_value,
            "funds_value": summary.funds_value,
            "gross_value": summary.gross_value,
            "capital_gain": summary.capital_gain,
            "tax_rate": summary.tax_rate,
            "tax_amount": summary.tax_amount,
            "net_value": summary.net_value,
            "gross_irr_pct": summary.gross_irr_pct,
            "net_irr_pct": summary.net_irr_pct,
            "premiums": summary.premiums_json(),
            "holdings": summary.holdings_json(),
        }

        with self._session_factory() as session, session.begin():
            snapshot = session.scalar(
                select(PolicySnapshot).where(PolicySnapshot.as_of == summary.as_of)
            )
            if snapshot is None:
                snapshot = PolicySnapshot(as_of=summary.as_of, **values)
                session.add(snapshot)
            else:
                for key, value in values.items():
                    setattr(snapshot, key, value)
                snapshot.updated_at = utcnow()
            session.flush()
            return snapshot.id

    def get_policy_snapshot(self, as_of: date | None = None) -> PolicySnapshot:
        """Return the snapshot for ``as_of``, or the latest one when omitted."""

        with self._session_factory() as session:
            statement = select(PolicySnapshot)
            if as_of is None:
                statement = statement.order_by(PolicySnapshot.as_of.desc())
            else:
                statement = statement.where(PolicySnapshot.as_of == as_of)
            snapshot = session.scalars(statement).first()

        if snapshot is None:
            target = as_of.isoformat() if as_of is not None else "any date"
            msg = f"no policy snapshot stored for {target}"
            raise PolicySnapshotNotFoundError(msg)
        return snapshot

    def list_policy_snapshots(self) -> pl.DataFrame:
        statement = select(
            PolicySnapshot.id,
            PolicySnapshot.as_of,
            PolicySnapshot.currency,
            PolicySnapshot.total_premiums,
            PolicySnapshot.gesav_value,
            PolicySnapshot.funds_value,
            PolicySnapshot.gross_value,
            PolicySnapshot.capital_gain,
            PolicySnapshot.tax_rate,
            PolicySnapshot.tax_amount,
            PolicySnapshot.net_value,
            PolicySnapshot.gross_irr_pct,
            PolicySnapshot.net_irr_pct,
            PolicySnapshot.created_at,
        ).order_by(PolicySnapshot.as_of.desc())
        with self._session_factory() as session:
            rows = session.execute(statement).all()
        return _rows_to_frame(rows, POLICY_COLUMNS, POLICY_SCHEMA)
