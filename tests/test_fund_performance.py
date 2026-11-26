"""Tests for the fund-versus-premium comparison."""

from __future__ import annotations

from datetime import date

import pytest

from assetlens.services.fund_performance import FundPrices, compare_fund_performance
from assetlens.services.policy import Premium


def _fund() -> FundPrices:
    return FundPrices(
        symbol="GI_GEN_TUTELA_CLIMA",
        name="Generali Tutela del Clima",
        points=[
            (date(2024, 1, 2), 90.0),
            (date(2024, 4, 18), 100.0),
            (date(2024, 4, 22), 110.0),
            (date(2026, 11, 25), 121.0),
        ],
    )


def test_comparison_uses_last_observation_on_or_before_premium() -> None:
    premiums = [
        Premium(date(2024, 4, 19), 1000.0),
        Premium(date(2025, 4, 19), 1000.0),
    ]

    report = compare_fund_performance(premiums, [_fund()])

    assert report.reference_date == date(2026, 11, 25)
    assert len(report.funds) == 1
    first, second = report.funds[0].comparisons
    assert first.paid_on == date(2024, 4, 19)
    assert first.nav_date == date(2024, 4, 18)
    assert first.nav_at_premium == pytest.approx(100.0)
    assert first.latest_nav == pytest.approx(121.0)
    assert first.change_pct == pytest.approx(21.0)

    assert second.nav_date == date(2024, 4, 22)
    assert second.nav_at_premium == pytest.approx(110.0)
    assert second.change_pct == pytest.approx(10.0)


def test_comparison_skips_premiums_outside_the_fund_history() -> None:
    premiums = [
        Premium(date(2023, 1, 1), 1000.0),
        Premium(date(2024, 4, 19), 1000.0),
    ]

    report = compare_fund_performance(premiums, [_fund()])

    assert [item.paid_on for item in report.funds[0].comparisons] == [date(2024, 4, 19)]


def test_comparison_reference_date_can_be_overridden() -> None:
    premiums = [Premium(date(2024, 4, 19), 1000.0)]

    report = compare_fund_performance(
        premiums,
        [_fund()],
        reference_date=date(2024, 4, 22),
    )

    assert report.reference_date == date(2024, 4, 22)
    assert report.funds[0].comparisons[0].latest_nav == pytest.approx(110.0)
    assert report.funds[0].comparisons[0].change_pct == pytest.approx(10.0)


def test_comparison_without_funds_has_no_reference_date() -> None:
    report = compare_fund_performance([Premium(date(2024, 4, 19), 1000.0)], [])

    assert report.reference_date is None
    assert report.funds == ()
