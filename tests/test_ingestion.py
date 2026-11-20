"""Tests for the ingestion orchestration."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

import polars as pl

from assetlens.config import ProviderConfig, Settings, Watchlist
from assetlens.db.repository import Repository
from assetlens.providers.base import BaseProvider
from assetlens.services.ingestion import IngestionService


class FakeProvider(BaseProvider):
    name = "fake"

    def __init__(self, frame: pl.DataFrame, *, fail: bool = False) -> None:
        self.frame = frame
        self.fail = fail
        self.calls: list[tuple[str, date, date]] = []

    def fetch_history(self, symbol: str, start: date, end: date) -> pl.DataFrame:
        self.calls.append((symbol, start, end))
        if self.fail:
            raise RuntimeError("provider exploded")
        return self.frame.filter((pl.col("price_date") >= start) & (pl.col("price_date") <= end))

    def close(self) -> None:
        return None


def _watchlist(**overrides: Any) -> Watchlist:
    return Watchlist(
        providers={"fake": ProviderConfig(base_url="https://example.invalid")},
        instruments=[
            {
                "provider": "fake",
                "symbol": "TEST",
                "name": "Test Instrument",
                "asset_class": "fund",
                **overrides,
            }
        ],
    )


def _service(
    repository: Repository,
    settings: Settings,
    provider: FakeProvider,
    **watchlist_overrides: Any,
) -> IngestionService:
    def factory(
        _name: str, _config: ProviderConfig, _settings: Settings, **_kwargs: Any
    ) -> FakeProvider:
        return provider

    return IngestionService(
        repository,
        _watchlist(**watchlist_overrides),
        settings,
        provider_factory=factory,
    )


def test_full_sync_inserts_prices(
    repository: Repository, settings: Settings, price_frame: pl.DataFrame
) -> None:
    provider = FakeProvider(price_frame)
    service = _service(repository, settings, provider)

    results = service.sync(full=True, end=date(2024, 1, 31))

    assert len(results) == 1
    assert results[0].status == "success"
    assert results[0].fetched == 4
    assert results[0].inserted == 4
    assert provider.calls[0][1] == settings.backfill_start
    assert repository.count_prices("TEST") == 4


def test_incremental_sync_uses_overlap(
    repository: Repository, settings: Settings, price_frame: pl.DataFrame
) -> None:
    provider = FakeProvider(price_frame)
    service = _service(repository, settings, provider)
    service.sync(full=True, end=date(2024, 1, 31))

    results = service.sync(end=date(2024, 1, 31))

    assert results[0].status == "success"
    assert results[0].inserted == 0
    assert results[0].updated == 4
    expected_start = date(2024, 1, 5) - timedelta(days=5)
    assert provider.calls[-1][1] == expected_start


def test_failure_is_recorded_without_raising(
    repository: Repository, settings: Settings, price_frame: pl.DataFrame
) -> None:
    provider = FakeProvider(price_frame, fail=True)
    service = _service(repository, settings, provider)

    results = service.sync(full=True, end=date(2024, 1, 31))

    assert results[0].status == "failed"
    assert results[0].error is not None
    assert "provider exploded" in results[0].error
    runs = repository.list_runs()
    assert runs["status"].to_list() == ["failed"]


def test_unknown_symbol_is_rejected(
    repository: Repository, settings: Settings, price_frame: pl.DataFrame
) -> None:
    provider = FakeProvider(price_frame)
    service = _service(repository, settings, provider)

    try:
        service.sync(symbols=["MISSING"], full=True)
    except KeyError as exc:
        assert "MISSING" in str(exc)
    else:  # pragma: no cover - defensive
        raise AssertionError("expected KeyError")


def test_full_sync_uses_instrument_backfill_start(
    repository: Repository, settings: Settings, price_frame: pl.DataFrame
) -> None:
    provider = FakeProvider(price_frame)
    service = _service(repository, settings, provider, backfill_start=date(1981, 1, 1))

    service.sync(full=True, end=date(2024, 1, 31))

    assert provider.calls[0][1] == date(1981, 1, 1)


def test_monthly_incremental_sync_uses_a_wider_overlap(
    repository: Repository, settings: Settings, price_frame: pl.DataFrame
) -> None:
    provider = FakeProvider(price_frame)
    service = _service(repository, settings, provider, frequency="monthly", value_type="return_pct")
    service.sync(full=True, end=date(2024, 1, 31))

    service.sync(end=date(2024, 1, 31))

    assert provider.calls[-1][1] == date(2024, 1, 5) - timedelta(days=40)
