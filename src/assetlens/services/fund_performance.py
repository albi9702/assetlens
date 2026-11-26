"""Fund performance at the premium dates.

For every premium paid into the policy, compare the NAV of each unit-linked
compartment on (or just before) that date with its latest NAV. The result is a
matrix of funds by premium dates, which the dashboard renders as a heatmap and
a grouped bar chart.

GESAV is intentionally absent: it is a separate account with a yearly return,
not a quoted fund, so it cannot be compared on NAV.
"""

from __future__ import annotations

from bisect import bisect_right
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date

from assetlens.services.policy import Premium


@dataclass(frozen=True, slots=True)
class FundPrices:
    """One fund and its full NAV history, sorted by date."""

    symbol: str
    name: str
    points: Sequence[tuple[date, float]]


@dataclass(frozen=True, slots=True)
class FundPremiumComparison:
    """NAV at one premium date versus the latest available NAV."""

    paid_on: date
    nav_at_premium: float
    nav_date: date
    latest_nav: float
    latest_date: date
    change_pct: float


@dataclass(frozen=True, slots=True)
class FundPerformance:
    """The comparisons of a single fund across the premium dates."""

    symbol: str
    name: str
    comparisons: tuple[FundPremiumComparison, ...]


@dataclass(frozen=True, slots=True)
class FundPerformanceReport:
    """Fund comparisons for the whole policy."""

    reference_date: date | None
    premiums: tuple[Premium, ...]
    funds: tuple[FundPerformance, ...]


def _last_on_or_before(
    points: Sequence[tuple[date, float]], target: date
) -> tuple[date, float] | None:
    """Return the last observation whose date is not after ``target``."""

    dates = [point[0] for point in points]
    index = bisect_right(dates, target) - 1
    if index < 0:
        return None
    return points[index]


def compare_fund_performance(
    premiums: Sequence[Premium],
    funds: Sequence[FundPrices],
    *,
    reference_date: date | None = None,
) -> FundPerformanceReport:
    """Compare every fund's NAV at each premium date with its latest NAV.

    ``reference_date`` is the common "today" of the comparison. When omitted it
    is the most recent observation across all funds, so a page refresh does not
    depend on the wall clock. A fund whose last observation is older than the
    reference date still uses its own latest value, and the API exposes that
    date to the caller.
    """

    ordered_premiums = tuple(sorted(premiums, key=lambda premium: premium.paid_on))
    normalized_funds = tuple(
        FundPrices(
            symbol=fund.symbol,
            name=fund.name,
            points=tuple(sorted(fund.points, key=lambda point: point[0])),
        )
        for fund in funds
        if fund.points
    )

    if reference_date is None:
        reference_date = max(
            (fund.points[-1][0] for fund in normalized_funds),
            default=None,
        )

    results: list[FundPerformance] = []
    for fund in normalized_funds:
        if reference_date is None:
            continue
        latest = _last_on_or_before(fund.points, reference_date)
        if latest is None:
            continue
        latest_date, latest_nav = latest

        comparisons: list[FundPremiumComparison] = []
        for premium in ordered_premiums:
            if premium.paid_on > reference_date:
                continue
            at_premium = _last_on_or_before(fund.points, premium.paid_on)
            if at_premium is None:
                continue
            nav_date, nav = at_premium
            if nav <= 0:
                continue
            comparisons.append(
                FundPremiumComparison(
                    paid_on=premium.paid_on,
                    nav_at_premium=round(nav, 6),
                    nav_date=nav_date,
                    latest_nav=round(latest_nav, 6),
                    latest_date=latest_date,
                    change_pct=round((latest_nav / nav - 1.0) * 100.0, 4),
                )
            )

        results.append(
            FundPerformance(
                symbol=fund.symbol,
                name=fund.name,
                comparisons=tuple(comparisons),
            )
        )

    return FundPerformanceReport(
        reference_date=reference_date,
        premiums=ordered_premiums,
        funds=tuple(results),
    )
