"""Tests for Polars analytics."""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from assetlens.services.analytics import compute_rate_summary, compute_summary


def _frame(rows: list[tuple[date, float]]) -> pl.DataFrame:
    return pl.DataFrame(
        {
            "price_date": [row[0] for row in rows],
            "value": [row[1] for row in rows],
        },
        schema={"price_date": pl.Date, "value": pl.Float64},
    )


def test_summary_metrics() -> None:
    frame = _frame(
        [
            (date(2024, 1, 1), 100.0),
            (date(2024, 2, 1), 110.0),
            (date(2024, 3, 1), 121.0),
            (date(2024, 4, 1), 133.1),
        ]
    )

    summary = compute_summary(frame, symbol="TEST")

    assert summary.observations == 4
    assert summary.last_value == pytest.approx(133.1)
    assert summary.total_return_pct == pytest.approx(33.1)
    assert summary.return_1m_pct == pytest.approx(10.0)
    assert summary.return_3m_pct == pytest.approx(33.1)
    assert summary.cagr_pct is not None
    assert summary.volatility_pct is not None


def test_max_drawdown_is_negative() -> None:
    frame = _frame(
        [
            (date(2024, 1, 1), 100.0),
            (date(2024, 1, 2), 120.0),
            (date(2024, 1, 3), 90.0),
            (date(2024, 1, 4), 110.0),
        ]
    )

    summary = compute_summary(frame, symbol="TEST")

    assert summary.max_drawdown_pct == pytest.approx(-25.0)
    assert summary.total_return_pct == pytest.approx(10.0)


def test_summary_handles_single_observation() -> None:
    frame = _frame([(date(2024, 1, 1), 100.0)])

    summary = compute_summary(frame, symbol="TEST")

    assert summary.total_return_pct == 0.0
    assert summary.cagr_pct is None
    assert summary.volatility_pct is None
    assert summary.return_1m_pct is None


def test_summary_rejects_empty_frame() -> None:
    with pytest.raises(ValueError, match="no prices"):
        compute_summary(
            pl.DataFrame(schema={"price_date": pl.Date, "value": pl.Float64}), symbol="X"
        )


def test_summary_as_dict() -> None:
    frame = _frame([(date(2024, 1, 1), 100.0), (date(2024, 2, 1), 110.0)])

    payload = compute_summary(frame, symbol="TEST").as_dict()

    assert payload["symbol"] == "TEST"
    assert payload["last_date"] == date(2024, 2, 1)


def test_rate_summary_compounds_annual_returns() -> None:
    frame = _frame(
        [
            (date(2020, 1, 1), 10.0),
            (date(2020, 12, 1), 10.0),
            (date(2021, 1, 1), -10.0),
            (date(2021, 12, 1), -10.0),
            (date(2022, 1, 1), 21.0),
            (date(2022, 12, 1), 21.0),
        ]
    )

    summary = compute_rate_summary(frame, symbol="GESAV")

    assert summary.years == 3
    assert summary.latest_year == 2022
    assert summary.latest_return_pct == pytest.approx(21.0)
    assert summary.compounded_return_pct == pytest.approx((1.1 * 0.9 * 1.21 - 1.0) * 100.0)
    assert summary.average_return_pct == pytest.approx(7.0)
    assert summary.best_year == 2022
    assert summary.worst_year == 2021


def test_rate_summary_uses_the_last_observation_of_each_year() -> None:
    frame = _frame(
        [
            (date(2024, 1, 1), 2.0),
            (date(2024, 6, 1), 3.0),
            (date(2024, 12, 1), 4.0),
            (date(2025, 1, 1), 5.0),
        ]
    )

    summary = compute_rate_summary(frame, symbol="GESAV")

    assert summary.years == 2
    assert summary.compounded_return_pct == pytest.approx((1.04 * 1.05 - 1.0) * 100.0)
    assert summary.latest_year == 2025
    assert summary.latest_return_pct == pytest.approx(5.0)


def test_rate_summary_handles_a_single_year() -> None:
    frame = _frame([(date(2024, 1, 1), 3.0), (date(2024, 12, 1), 3.5)])

    summary = compute_rate_summary(frame, symbol="GESAV")

    assert summary.years == 1
    assert summary.compounded_return_pct == pytest.approx(3.5)
    assert summary.cagr_pct is None
    assert summary.volatility_pct is None


def test_rate_summary_rejects_empty_frame() -> None:
    with pytest.raises(ValueError, match="no observations"):
        compute_rate_summary(
            pl.DataFrame(schema={"price_date": pl.Date, "value": pl.Float64}), symbol="GESAV"
        )
