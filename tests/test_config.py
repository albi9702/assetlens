"""Tests for configuration loading."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from assetlens.config import (
    InstrumentConfig,
    Settings,
    Watchlist,
    load_watchlist,
    normalize_database_url,
    project_root,
    redact_database_url,
)

EXPECTED_SYMBOLS = {
    "GI_GEN_TUTELA_CLIMA",
    "GI_GEN_CONSUMO_RESPONSABILE",
    "GI_GEN_PARI_OPPORTUNITA",
    "GI_GEN_SALUTE_BENESSERE",
    "GI_GEN_CRESCITA_SOSTENIBILE",
    "GESAV",
}


def test_repository_watchlist_is_valid() -> None:
    path = project_root() / "config" / "instruments.yaml"
    watchlist = load_watchlist(path)

    assert {instrument.symbol for instrument in watchlist.instruments} == EXPECTED_SYMBOLS
    assert all(instrument.provider == "generali" for instrument in watchlist.instruments)

    gesav = watchlist.by_symbol("GESAV")
    assert gesav.asset_class == "separate_account"
    assert gesav.frequency == "monthly"
    assert gesav.value_type == "return_pct"
    assert gesav.backfill_start == date(1981, 1, 1)


def test_missing_file_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        load_watchlist(tmp_path / "missing.yaml")


def test_unknown_provider_is_rejected() -> None:
    with pytest.raises(ValueError, match="unknown providers"):
        Watchlist.model_validate(
            {
                "providers": {"generali": {"base_url": "https://example.com"}},
                "instruments": [
                    {
                        "provider": "other",
                        "symbol": "X",
                        "name": "X",
                    }
                ],
            }
        )


def test_duplicate_instrument_is_rejected() -> None:
    with pytest.raises(ValueError, match="duplicate instrument"):
        Watchlist.model_validate(
            {
                "providers": {"generali": {"base_url": "https://example.com"}},
                "instruments": [
                    {"provider": "generali", "symbol": "X", "name": "X"},
                    {"provider": "generali", "symbol": "X", "name": "X"},
                ],
            }
        )


def test_instrument_defaults_are_price_and_daily() -> None:
    instrument = InstrumentConfig(provider="generali", symbol="X", name="X")

    assert instrument.frequency == "daily"
    assert instrument.value_type == "price"


def test_instrument_accepts_monthly_return_series() -> None:
    instrument = InstrumentConfig(
        provider="generali",
        symbol="GESAV",
        name="GESAV",
        asset_class="separate_account",
        frequency="monthly",
        value_type="return_pct",
    )

    assert instrument.frequency == "monthly"
    assert instrument.value_type == "return_pct"


def test_postgres_urls_use_psycopg() -> None:
    assert normalize_database_url("postgresql://user:pass@host/db") == (
        "postgresql+psycopg://user:pass@host/db"
    )
    assert normalize_database_url("postgres://user:pass@host/db") == (
        "postgresql+psycopg://user:pass@host/db"
    )
    assert normalize_database_url("postgresql+psycopg://user:pass@host/db") == (
        "postgresql+psycopg://user:pass@host/db"
    )
    assert normalize_database_url("sqlite:///data/test.db") == "sqlite:///data/test.db"


def test_database_urls_are_redacted() -> None:
    url = "postgresql+psycopg://user:secret@host/db?sslmode=require"

    assert redact_database_url(url) == "postgresql+psycopg://user:***@host/db?sslmode=require"


def test_migration_url_defaults_to_the_application_url() -> None:
    database_url = "postgresql://user:secret@host/db"
    settings = Settings(_env_file=None, database_url=database_url)

    assert settings.resolved_database_url == "postgresql+psycopg://user:secret@host/db"
    assert settings.resolved_migration_database_url == settings.resolved_database_url


def test_migration_url_can_use_a_direct_connection() -> None:
    settings = Settings(
        _env_file=None,
        database_url="postgresql://user:secret@host-pooler/db",
        migration_database_url="postgresql://user:secret@host/db",
    )

    assert "-pooler" in settings.resolved_database_url
    assert "-pooler" not in settings.resolved_migration_database_url
    assert "secret" not in settings.redacted_database_url
    assert "secret" not in settings.redacted_migration_database_url
