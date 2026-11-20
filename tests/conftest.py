"""Shared pytest fixtures."""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import date
from pathlib import Path

import polars as pl
import pytest
from sqlalchemy import Engine

from assetlens.config import InstrumentConfig, ProviderConfig, Settings, Watchlist
from assetlens.db.engine import create_all, create_db_engine
from assetlens.db.repository import Repository

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        data_dir=tmp_path,
        database_url=f"sqlite:///{tmp_path / 'test.db'}",
        instruments_file=tmp_path / "instruments.yaml",
        request_delay_seconds=0,
        max_retries=2,
    )


@pytest.fixture
def engine(settings: Settings) -> Iterator[Engine]:
    engine = create_db_engine(settings.resolved_database_url)
    create_all(engine)
    yield engine
    engine.dispose()


@pytest.fixture
def repository(engine: Engine) -> Repository:
    return Repository(engine)


@pytest.fixture
def watchlist() -> Watchlist:
    return Watchlist(
        providers={
            "generali": ProviderConfig(base_url="https://asset.generali.it"),
        },
        instruments=[
            InstrumentConfig(
                provider="generali",
                symbol="GI_GEN_TUTELA_CLIMA",
                name="Generali Tutela del Clima",
                asset_class="fund",
                currency="EUR",
                url="https://asset.generali.it/quotazioni/genera-sviluppo-sostenibile",
                metadata={"umbrella": "Genera Sviluppo Sostenibile"},
            )
        ],
    )


@pytest.fixture
def price_frame() -> pl.DataFrame:
    return pl.DataFrame(
        {
            "price_date": [
                date(2024, 1, 2),
                date(2024, 1, 3),
                date(2024, 1, 4),
                date(2024, 1, 5),
            ],
            "value": [10.0, 10.1, 9.9, 10.2],
        },
        schema={"price_date": pl.Date, "value": pl.Float64},
    )


@pytest.fixture
def generali_payload() -> list[dict[str, object]]:
    data = json.loads((FIXTURES / "generali_payload.json").read_text(encoding="utf-8"))
    return data["payload"]


@pytest.fixture
def gesav_payload() -> list[dict[str, object]]:
    data = json.loads((FIXTURES / "gesav_payload.json").read_text(encoding="utf-8"))
    return data["payload"]
