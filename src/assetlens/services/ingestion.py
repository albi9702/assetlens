"""Ingestion orchestration.

The service resolves the watchlist, computes the date window for each
instrument, calls the provider, upserts the result and records an audit row in
``ingestion_runs``. A failure on one instrument never stops the others.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Literal
from zoneinfo import ZoneInfo

from assetlens.config import InstrumentConfig, Settings, Watchlist
from assetlens.db.repository import Repository
from assetlens.logging import get_logger
from assetlens.providers.base import BaseProvider
from assetlens.providers.registry import build_provider

ROME = ZoneInfo("Europe/Rome")
# Monthly and lower-frequency sources publish with a lag, so a fixed
# five-day overlap could miss the latest observation after a month.
FREQUENCY_OVERLAP_DAYS: dict[str, int] = {
    "daily": 0,
    "weekly": 14,
    "monthly": 40,
    "quarterly": 100,
    "yearly": 370,
}
logger = get_logger(__name__)

ProviderFactory = Callable[..., BaseProvider]


def today_in_rome() -> date:
    return datetime.now(ROME).date()


@dataclass(frozen=True, slots=True)
class SyncResult:
    provider: str
    symbol: str
    status: Literal["success", "failed"]
    requested_from: date
    requested_to: date
    fetched: int = 0
    inserted: int = 0
    updated: int = 0
    error: str | None = None
    run_id: int | None = None


class IngestionService:
    def __init__(
        self,
        repository: Repository,
        watchlist: Watchlist,
        settings: Settings,
        *,
        provider_factory: ProviderFactory = build_provider,
    ) -> None:
        self.repository = repository
        self.watchlist = watchlist
        self.settings = settings
        self._provider_factory = provider_factory

    def sync(
        self,
        *,
        symbols: Sequence[str] | None = None,
        provider: str | None = None,
        full: bool = False,
        start: date | None = None,
        end: date | None = None,
    ) -> list[SyncResult]:
        """Sync the selected instruments and return one result per instrument."""

        instruments = self._select(symbols=symbols, provider=provider)
        providers: dict[str, BaseProvider] = {}
        results: list[SyncResult] = []

        try:
            for instrument in instruments:
                provider_name = instrument.provider
                if provider_name not in providers:
                    providers[provider_name] = self._provider_factory(
                        provider_name,
                        self.watchlist.providers[provider_name],
                        self.settings,
                    )
                results.append(
                    self._sync_one(
                        instrument,
                        providers[provider_name],
                        full=full,
                        start=start,
                        end=end,
                    )
                )
        finally:
            for provider_instance in providers.values():
                provider_instance.close()

        return results

    def _select(
        self, *, symbols: Sequence[str] | None, provider: str | None
    ) -> list[InstrumentConfig]:
        instruments = self.watchlist.active_instruments()
        if provider is not None:
            instruments = [
                instrument for instrument in instruments if instrument.provider == provider
            ]
        if symbols:
            wanted = set(symbols)
            available = {instrument.symbol for instrument in instruments}
            missing = wanted - available
            if missing:
                msg = f"symbols not found in watchlist: {sorted(missing)}"
                raise KeyError(msg)
            instruments = [instrument for instrument in instruments if instrument.symbol in wanted]
        if not instruments:
            msg = "no active instruments match the selection"
            raise ValueError(msg)
        return instruments

    def _resolve_window(
        self,
        instrument: InstrumentConfig,
        instrument_id: int,
        *,
        full: bool,
        start: date | None,
        end: date | None,
    ) -> tuple[date, date]:
        requested_to = end or today_in_rome()
        backfill_start = instrument.backfill_start or self.settings.backfill_start
        if start is not None:
            requested_from = start
        elif full:
            requested_from = backfill_start
        else:
            latest = self.repository.latest_price_date(instrument_id)
            overlap = max(
                self.settings.overlap_days,
                FREQUENCY_OVERLAP_DAYS.get(instrument.frequency, 0),
            )
            requested_from = backfill_start if latest is None else latest - timedelta(days=overlap)
        return requested_from, requested_to

    def _sync_one(
        self,
        instrument: InstrumentConfig,
        provider: BaseProvider,
        *,
        full: bool,
        start: date | None,
        end: date | None,
    ) -> SyncResult:
        instrument_id = self.repository.upsert_instrument(instrument)
        requested_from, requested_to = self._resolve_window(
            instrument, instrument_id, full=full, start=start, end=end
        )
        run_id = self.repository.start_run(
            provider=instrument.provider,
            symbol=instrument.symbol,
            instrument_id=instrument_id,
            requested_from=requested_from,
            requested_to=requested_to,
        )

        logger.info(
            "Syncing %s (%s): %s -> %s",
            instrument.symbol,
            instrument.provider,
            requested_from.isoformat(),
            requested_to.isoformat(),
        )

        try:
            if requested_from > requested_to:
                msg = f"invalid window: {requested_from} > {requested_to}"
                raise ValueError(msg)

            frame = provider.fetch_history(instrument.symbol, requested_from, requested_to)
            stats = self.repository.upsert_prices(
                instrument_id,
                frame,
                source=instrument.provider,
                value_type=instrument.value_type,
            )
            self.repository.finish_run(
                run_id,
                status="success",
                rows_fetched=frame.height,
                rows_inserted=stats.inserted,
                rows_updated=stats.updated,
            )
            logger.info(
                "Synced %s: %d rows (%d new, %d refreshed)",
                instrument.symbol,
                frame.height,
                stats.inserted,
                stats.updated,
            )
            return SyncResult(
                provider=instrument.provider,
                symbol=instrument.symbol,
                status="success",
                requested_from=requested_from,
                requested_to=requested_to,
                fetched=frame.height,
                inserted=stats.inserted,
                updated=stats.updated,
                run_id=run_id,
            )
        except Exception as exc:
            message = f"{type(exc).__name__}: {exc}"
            logger.exception("Failed to sync %s", instrument.symbol)
            self.repository.finish_run(run_id, status="failed", error=message)
            return SyncResult(
                provider=instrument.provider,
                symbol=instrument.symbol,
                status="failed",
                requested_from=requested_from,
                requested_to=requested_to,
                error=message,
                run_id=run_id,
            )
