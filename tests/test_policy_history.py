"""Tests for the daily reconstruction of a policy snapshot."""

from __future__ import annotations

from datetime import date

import pytest

from assetlens.services.policy import Holding, Premium
from assetlens.services.policy_history import (
    normalize_name,
    normalize_policy_name,
    reconstruct_daily_policy,
)

AS_OF = date(2024, 1, 10)


def test_normalize_name_ignores_case_accents_and_punctuation() -> None:
    assert normalize_name("Generali Pari Opportunità") == "generalipariopportunita"
    assert normalize_name("GESAV - Gestione Separata") == "gesavgestioneseparata"
    assert normalize_name("GI_GEN_TUTELA_CLIMA") == "gigentutelaclima"
    assert normalize_policy_name("Generali Tutela del Clima") == "tutelaclima"
    assert normalize_policy_name("GI_GEN_TUTELA_CLIMA") == "tutelaclima"
    assert normalize_policy_name("GESAV") == "gesav"


def test_daily_series_uses_the_snapshot_as_anchor() -> None:
    points = reconstruct_daily_policy(
        as_of=AS_OF,
        premiums=[
            Premium(date(2023, 1, 1), 1000.00),
            Premium(date(2024, 1, 1), 1000.00),
        ],
        holdings=[
            Holding("GESAV", 600.00),
            Holding("Generali Tutela del Clima", 400.00, quote=10.0, nav=40.0),
        ],
        prices={
            "GESAV": [(date(2023, 1, 1), 3.0), (date(2024, 1, 1), 3.0)],
            "GI_GEN_TUTELA_CLIMA": [
                (date(2023, 1, 1), 20.0),
                (date(2024, 1, 1), 40.0),
                (date(2024, 1, 11), 44.0),
            ],
        },
        gesav_symbol="GESAV",
        start=date(2023, 12, 30),
        end=date(2024, 1, 11),
    )

    by_date = {point.date: point for point in points}
    assert len(points) == 13
    assert points[0].date == date(2023, 12, 30)
    assert points[-1].date == date(2024, 1, 11)

    assert by_date[date(2023, 12, 31)].invested == pytest.approx(1000.00)
    assert by_date[date(2024, 1, 1)].invested == pytest.approx(2000.00)

    assert by_date[AS_OF].gesav_value == pytest.approx(600.00, abs=0.01)
    assert by_date[AS_OF].funds_value == pytest.approx(400.00, abs=0.01)
    assert by_date[AS_OF].gross_value == pytest.approx(1000.00, abs=0.01)
    assert by_date[AS_OF].net_value == pytest.approx(1000.00, abs=0.01)

    assert by_date[date(2024, 1, 11)].funds_value == pytest.approx(440.00, abs=0.01)
    assert by_date[date(2024, 1, 11)].gross_value > by_date[AS_OF].gross_value


def test_first_premium_starts_at_the_amount_paid() -> None:
    points = reconstruct_daily_policy(
        as_of=date(2025, 1, 1),
        premiums=[
            Premium(date(2024, 1, 1), 5000.00),
            Premium(date(2025, 1, 1), 1000.00),
        ],
        holdings=[
            Holding("GESAV", 3600.00),
            Holding("Generali Tutela del Clima", 3000.00, quote=10.0, nav=30.0),
        ],
        prices={
            "GESAV": [(date(2024, 1, 1), 3.0), (date(2025, 1, 1), 3.0)],
            "GI_GEN_TUTELA_CLIMA": [(date(2024, 1, 1), 10.0), (date(2025, 1, 1), 15.0)],
        },
        gesav_symbol="GESAV",
        start=date(2024, 1, 1),
        end=date(2025, 1, 1),
        annual_rate=0.05,
    )

    first, last = points[0], points[-1]
    assert first.invested == pytest.approx(5000.00)
    assert first.gross_value == pytest.approx(5000.00, abs=0.01)
    assert first.net_value == pytest.approx(5000.00, abs=0.01)
    assert last.gross_value == pytest.approx(6600.00, abs=0.01)
    assert last.net_value == pytest.approx(6480.00, abs=0.01)


def test_daily_series_taxes_only_the_gain() -> None:
    points = reconstruct_daily_policy(
        as_of=AS_OF,
        premiums=[Premium(date(2024, 1, 1), 1000.00)],
        holdings=[Holding("GESAV", 900.00)],
        prices={"GESAV": [(date(2024, 1, 1), 3.0)]},
        gesav_symbol="GESAV",
        start=AS_OF,
        end=AS_OF,
    )

    assert points[0].gross_value == pytest.approx(900.00, abs=0.01)
    assert points[0].net_value == pytest.approx(900.00, abs=0.01)


def test_daily_series_rejects_invalid_range() -> None:
    with pytest.raises(ValueError, match="start"):
        reconstruct_daily_policy(
            as_of=AS_OF,
            premiums=[Premium(date(2024, 1, 1), 1000.00)],
            holdings=[Holding("GESAV", 1000.00)],
            prices={},
            gesav_symbol="GESAV",
            start=date(2024, 1, 11),
            end=date(2024, 1, 10),
        )

    with pytest.raises(ValueError, match="end"):
        reconstruct_daily_policy(
            as_of=AS_OF,
            premiums=[Premium(date(2024, 1, 1), 1000.00)],
            holdings=[Holding("GESAV", 1000.00)],
            prices={},
            gesav_symbol="GESAV",
            start=date(2024, 1, 8),
            end=date(2024, 1, 9),
        )
