"""Tests for the read-only FastAPI backend."""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest
from fastapi.testclient import TestClient

from assetlens.api.app import create_app
from assetlens.config import InstrumentConfig, Settings
from assetlens.db.repository import Repository
from assetlens.services.policy import Holding, Premium, compute_policy_summary


def _seed(repository: Repository, watchlist, price_frame: pl.DataFrame) -> None:
    instrument_id = repository.upsert_instrument(watchlist.instruments[0])
    repository.upsert_prices(instrument_id, price_frame, source="generali")


def test_health_and_instruments(
    settings: Settings, repository: Repository, watchlist, price_frame: pl.DataFrame
) -> None:
    _seed(repository, watchlist, price_frame)
    app = create_app(settings)

    with TestClient(app) as client:
        health = client.get("/health")
        instruments = client.get("/instruments")

    assert health.status_code == 200
    assert health.json()["instruments"] == 1
    assert health.json()["prices"] == 4
    assert instruments.status_code == 200
    payload = instruments.json()
    assert len(payload) == 1
    assert payload[0]["symbol"] == "GI_GEN_TUTELA_CLIMA"
    assert payload[0]["observations"] == 4


def test_prices_and_summary(
    settings: Settings, repository: Repository, watchlist, price_frame: pl.DataFrame
) -> None:
    _seed(repository, watchlist, price_frame)
    app = create_app(settings)

    with TestClient(app) as client:
        prices = client.get("/instruments/GI_GEN_TUTELA_CLIMA/prices", params={"limit": 2})
        summary = client.get("/instruments/GI_GEN_TUTELA_CLIMA/summary")
        detail = client.get("/instruments/GI_GEN_TUTELA_CLIMA")

    assert prices.status_code == 200
    rows = prices.json()
    assert [row["price_date"] for row in rows] == ["2024-01-04", "2024-01-05"]
    assert summary.status_code == 200
    assert summary.json()["symbol"] == "GI_GEN_TUTELA_CLIMA"
    assert detail.status_code == 200
    assert detail.json()["metadata"]["umbrella"] == "Genera Sviluppo Sostenibile"


def _seed_policy(repository: Repository):
    summary = compute_policy_summary(
        [
            Premium(date(2024, 1, 1), 1000.00),
            Premium(date(2025, 1, 1), 1000.00),
        ],
        [
            Holding("GESAV", 1500.00),
            Holding("Generali Tutela del Clima", 700.00, 10.0, 70.0),
        ],
        as_of=date(2026, 1, 1),
    )
    repository.upsert_policy_snapshot(summary)
    return summary


def test_policy_endpoints(settings: Settings, repository: Repository) -> None:
    summary = _seed_policy(repository)
    app = create_app(settings)

    with TestClient(app) as client:
        snapshots = client.get("/policy/snapshots")
        analysis = client.get("/policy/analysis")
        selected = client.get("/policy/analysis", params={"as_of": "2026-01-01"})
        missing = client.get("/policy/analysis", params={"as_of": "2000-01-01"})

    assert snapshots.status_code == 200
    series = snapshots.json()
    assert len(series) == 1
    assert series[0]["as_of"] == "2026-01-01"
    assert series[0]["gross_value"] == pytest.approx(summary.gross_value)

    assert analysis.status_code == 200
    body = analysis.json()
    assert body["snapshot"]["net_value"] == pytest.approx(summary.net_value)
    assert len(body["gross_tranches"]) == 2
    assert body["gross_tranches"][0]["weight_pct"] == pytest.approx(50.0)
    assert sum(t["projected_value"] for t in body["gross_tranches"]) == pytest.approx(
        summary.gross_value, abs=0.05
    )
    assert sum(t["projected_value"] for t in body["net_tranches"]) == pytest.approx(
        summary.net_value, abs=0.05
    )

    assert selected.status_code == 200
    assert selected.json()["snapshot"]["as_of"] == "2026-01-01"
    assert missing.status_code == 404


def test_policy_daily_endpoint(settings: Settings, repository: Repository) -> None:
    fund_id = repository.upsert_instrument(
        InstrumentConfig(
            provider="generali",
            symbol="GI_GEN_TUTELA_CLIMA",
            name="Generali Tutela del Clima",
            asset_class="fund",
            currency="EUR",
            value_type="price",
            metadata={},
        )
    )
    gesav_id = repository.upsert_instrument(
        InstrumentConfig(
            provider="generali",
            symbol="GESAV",
            name="GESAV - Gestione Separata",
            asset_class="separate_account",
            currency="EUR",
            value_type="return_pct",
            metadata={},
        )
    )
    repository.upsert_prices(
        fund_id,
        pl.DataFrame(
            {
                "price_date": [date(2026, 4, 17), date(2026, 11, 16)],
                "value": [10.0, 11.0],
            },
            schema={"price_date": pl.Date, "value": pl.Float64},
        ),
        source="test",
    )
    repository.upsert_prices(
        gesav_id,
        pl.DataFrame(
            {
                "price_date": [date(2026, 4, 1), date(2026, 11, 1)],
                "value": [3.0, 3.0],
            },
            schema={"price_date": pl.Date, "value": pl.Float64},
        ),
        source="test",
    )
    repository.upsert_policy_snapshot(
        compute_policy_summary(
            [Premium(date(2026, 4, 2), 1000.00)],
            [
                Holding("GESAV", 600.00),
                Holding("Generali Tutela del Clima", 400.00, quote=40.0, nav=10.0),
            ],
            as_of=date(2026, 4, 2),
        )
    )
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/policy/daily")

    assert response.status_code == 200
    points = response.json()
    assert points[0]["date"] == "2026-04-02"
    assert points[0]["invested"] == pytest.approx(1000.00)
    assert points[0]["gross_value"] == pytest.approx(1000.00, abs=0.1)
    assert points[0]["net_value"] == pytest.approx(1000.00, abs=0.1)
    assert points[-1]["date"] == "2026-11-16"
    assert points[-1]["funds_value"] == pytest.approx(440.00, abs=0.1)
    assert points[-1]["gross_value"] > points[0]["gross_value"]


def test_market_history_ranges(settings: Settings, repository: Repository, watchlist) -> None:
    fund_id = repository.upsert_instrument(watchlist.instruments[0])
    repository.upsert_prices(
        fund_id,
        pl.DataFrame(
            {
                "price_date": [
                    date(2025, 11, 1),
                    date(2026, 1, 15),
                    date(2026, 8, 1),
                    date(2026, 11, 16),
                ],
                "value": [10.0, 10.5, 11.0, 11.5],
            },
            schema={"price_date": pl.Date, "value": pl.Float64},
        ),
        source="test",
    )
    gesav_id = repository.upsert_instrument(
        InstrumentConfig(
            provider="generali",
            symbol="GESAV",
            name="GESAV - Gestione Separata",
            asset_class="separate_account",
            currency="EUR",
            value_type="return_pct",
            metadata={},
        )
    )
    repository.upsert_prices(
        gesav_id,
        pl.DataFrame(
            {
                "price_date": [date(2025, 11, 1), date(2026, 4, 1), date(2026, 11, 1)],
                "value": [3.0, 3.1, 3.2],
            },
            schema={"price_date": pl.Date, "value": pl.Float64},
        ),
        source="test",
    )
    repository.upsert_policy_snapshot(
        compute_policy_summary(
            [Premium(date(2026, 4, 2), 1000.00)],
            [Holding("GESAV", 1000.00)],
            as_of=date(2026, 4, 2),
        )
    )
    app = create_app(settings)

    with TestClient(app) as client:
        responses = {
            period: client.get("/market/history", params={"range": period})
            for period in ("1y", "6m", "3m", "ytd", "premiums", "all")
        }

    counts = {
        period: len(response.json()["funds"][0]["points"]) for period, response in responses.items()
    }
    assert counts == {"1y": 3, "6m": 2, "3m": 1, "ytd": 3, "premiums": 0, "all": 4}
    all_data = responses["all"].json()
    assert all_data["reference_date"] == "2026-11-16"
    assert [series["symbol"] for series in all_data["funds"]] == ["GI_GEN_TUTELA_CLIMA"]
    assert len(all_data["gesav"]) == 3
    premiums_data = responses["premiums"].json()
    assert premiums_data["premium_dates"] == ["2026-04-02"]
    assert [point["date"] for point in premiums_data["gesav"]] == ["2026-04-01"]


def test_dashboard_is_served(settings: Settings) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        page = client.get("/dashboard/")
        redirect = client.get("/", follow_redirects=False)

    assert page.status_code == 200
    assert page.headers["content-type"].startswith("text/html")
    assert "Andamento della polizza" in page.text
    assert redirect.status_code in (302, 307)
    assert redirect.headers["location"] == "/dashboard/"


def test_unknown_instrument_returns_404(settings: Settings, engine) -> None:
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/instruments/DOES_NOT_EXIST")

    assert response.status_code == 404


def test_prices_filter_by_date(
    settings: Settings, repository: Repository, watchlist, price_frame: pl.DataFrame
) -> None:
    _seed(repository, watchlist, price_frame)
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get(
            "/instruments/GI_GEN_TUTELA_CLIMA/prices",
            params={"start": date(2024, 1, 3).isoformat(), "end": date(2024, 1, 4).isoformat()},
        )

    assert response.status_code == 200
    assert len(response.json()) == 2


def test_rate_series_summary(settings: Settings, repository: Repository) -> None:
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
            "price_date": [
                date(2024, 1, 1),
                date(2024, 12, 1),
                date(2025, 1, 1),
                date(2025, 12, 1),
            ],
            "value": [2.9, 3.0, 3.1, 3.2],
        },
        schema={"price_date": pl.Date, "value": pl.Float64},
    )
    repository.upsert_prices(instrument_id, frame, source="generali", value_type="return_pct")
    app = create_app(settings)

    with TestClient(app) as client:
        response = client.get("/instruments/GESAV/summary")

    assert response.status_code == 200
    payload = response.json()
    assert payload["value_type"] == "return_pct"
    assert payload["latest_year"] == 2025
    assert payload["latest_return_pct"] == 3.2
    assert payload["compounded_return_pct"] == pytest.approx((1.03 * 1.032 - 1.0) * 100.0)
    assert "total_return_pct" not in payload
