"""Smoke tests for the Typer CLI."""

from __future__ import annotations

from datetime import date

import polars as pl
from typer.testing import CliRunner

from assetlens.cli import app
from assetlens.config import InstrumentConfig, Settings
from assetlens.db.repository import Repository

runner = CliRunner()


def test_version() -> None:
    result = runner.invoke(app, ["version"])

    assert result.exit_code == 0
    assert "assetlens" in result.stdout


def test_instruments_and_prices(
    settings: Settings, repository: Repository, watchlist, price_frame: pl.DataFrame
) -> None:
    instrument_id = repository.upsert_instrument(watchlist.instruments[0])
    repository.upsert_prices(instrument_id, price_frame, source="generali")

    database = settings.resolved_database_url
    instruments = runner.invoke(app, ["--database", database, "instruments", "list"])
    prices = runner.invoke(
        app, ["--database", database, "prices", "show", "GI_GEN_TUTELA_CLIMA", "--tail", "2"]
    )

    assert instruments.exit_code == 0, instruments.stdout
    assert "GI_GEN_TUTELA_CLIMA" in instruments.stdout
    assert prices.exit_code == 0, prices.stdout
    assert "2024-01-05" in prices.stdout


def test_rate_series_analytics(settings: Settings, repository: Repository) -> None:
    instrument = InstrumentConfig(
        provider="generali",
        symbol="GESAV",
        name="GESAV",
        asset_class="separate_account",
        frequency="monthly",
        value_type="return_pct",
    )
    instrument_id = repository.upsert_instrument(instrument)
    frame = pl.DataFrame(
        {
            "price_date": [date(2024, 1, 1), date(2024, 12, 1)],
            "value": [2.9, 3.1],
        },
        schema={"price_date": pl.Date, "value": pl.Float64},
    )
    repository.upsert_prices(instrument_id, frame, source="generali", value_type="return_pct")

    result = runner.invoke(
        app,
        ["--database", settings.resolved_database_url, "analytics", "summary", "GESAV"],
    )

    assert result.exit_code == 0, result.stdout
    assert "return series" in result.stdout
    assert "Latest return" in result.stdout
