"""Performance analytics computed with Polars.

Every function accepts the normalized price frame returned by
:class:`~assetlens.db.repository.Repository` (``price_date`` and ``value``) and
never mutates it.
"""

from __future__ import annotations

import calendar
import math
from dataclasses import asdict, dataclass
from datetime import date
from typing import Any, Literal, cast

import polars as pl

TRADING_DAYS_PER_YEAR = 252
MIN_DAYS_FOR_CAGR = 30
MIN_RETURNS_FOR_VOLATILITY = 2


@dataclass(frozen=True, slots=True)
class PerformanceSummary:
    symbol: str
    first_date: date
    last_date: date
    observations: int
    first_value: float
    last_value: float
    total_return_pct: float
    cagr_pct: float | None
    volatility_pct: float | None
    max_drawdown_pct: float | None
    return_1m_pct: float | None
    return_3m_pct: float | None
    return_1y_pct: float | None
    return_ytd_pct: float | None
    value_type: Literal["price"] = "price"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True, slots=True)
class RateSummary:
    """Summary of a series that publishes annual percentage returns.

    Separate accounts such as GESAV do not have a NAV. They publish a
    return rate that is updated monthly and becomes final at year end, so
    the meaningful aggregation is the compounded product of the last
    observation of each calendar year.
    """

    symbol: str
    first_date: date
    last_date: date
    observations: int
    years: int
    latest_year: int
    latest_return_pct: float
    compounded_return_pct: float
    average_return_pct: float
    cagr_pct: float | None
    volatility_pct: float | None
    best_year: int
    best_year_pct: float
    worst_year: int
    worst_year_pct: float
    value_type: Literal["return_pct"] = "return_pct"

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _shift_months(day: date, months: int) -> date:
    total = day.year * 12 + (day.month - 1) - months
    year, month_index = divmod(total, 12)
    month = month_index + 1
    return date(year, month, min(day.day, calendar.monthrange(year, month)[1]))


def _value_on_or_before(frame: pl.DataFrame, target: date) -> float | None:
    values = frame.filter(pl.col("price_date") <= target)["value"]
    return float(cast(float, values[-1])) if values.len() else None


def _first_value_on_or_after(frame: pl.DataFrame, target: date) -> float | None:
    values = frame.filter(pl.col("price_date") >= target)["value"]
    return float(cast(float, values[0])) if values.len() else None


def _pct_change(start: float | None, end: float) -> float | None:
    if start is None or start == 0:
        return None
    return round((end / start - 1.0) * 100.0, 6)


def _round(value: float | None, digits: int = 6) -> float | None:
    return round(value, digits) if value is not None else None


def compute_summary(frame: pl.DataFrame, *, symbol: str) -> PerformanceSummary:
    """Compute a compact performance summary for a price history."""

    if frame.is_empty():
        msg = f"no prices available for {symbol}"
        raise ValueError(msg)

    required = {"price_date", "value"}
    if not required.issubset(frame.columns):
        msg = f"price frame must contain {sorted(required)}"
        raise ValueError(msg)

    data = (
        frame.select(["price_date", "value"])
        .drop_nulls()
        .unique(subset=["price_date"], keep="last")
        .sort("price_date")
    )
    if data.is_empty():
        msg = f"no valid prices available for {symbol}"
        raise ValueError(msg)

    first = data.row(0, named=True)
    last = data.row(-1, named=True)
    first_date: date = first["price_date"]
    last_date: date = last["price_date"]
    first_value = float(first["value"])
    last_value = float(last["value"])

    total_return = _pct_change(first_value, last_value) or 0.0

    elapsed_days = (last_date - first_date).days
    cagr: float | None = None
    if elapsed_days >= MIN_DAYS_FOR_CAGR and first_value > 0:
        cagr = _round(((last_value / first_value) ** (365.25 / elapsed_days) - 1.0) * 100.0)

    volatility: float | None = None
    daily_returns = data.select(pl.col("value").pct_change().alias("return")).drop_nulls()
    if daily_returns.height >= MIN_RETURNS_FOR_VOLATILITY:
        std = daily_returns["return"].std()
        if std is not None:
            volatility = _round(float(cast(float, std)) * math.sqrt(TRADING_DAYS_PER_YEAR) * 100.0)

    drawdowns = data.select((pl.col("value") / pl.col("value").cum_max() - 1.0).alias("drawdown"))
    max_drawdown = drawdowns["drawdown"].min()
    max_drawdown_pct = (
        _round(float(cast(float, max_drawdown)) * 100.0) if max_drawdown is not None else None
    )

    return PerformanceSummary(
        symbol=symbol,
        first_date=first_date,
        last_date=last_date,
        observations=data.height,
        first_value=first_value,
        last_value=last_value,
        total_return_pct=total_return,
        cagr_pct=cagr,
        volatility_pct=volatility,
        max_drawdown_pct=max_drawdown_pct,
        return_1m_pct=_pct_change(
            _value_on_or_before(data, _shift_months(last_date, 1)), last_value
        ),
        return_3m_pct=_pct_change(
            _value_on_or_before(data, _shift_months(last_date, 3)), last_value
        ),
        return_1y_pct=_pct_change(
            _value_on_or_before(data, _shift_months(last_date, 12)), last_value
        ),
        return_ytd_pct=_pct_change(
            _value_on_or_before(data, date(last_date.year - 1, 12, 31)), last_value
        )
        if first_date.year < last_date.year
        else None,
    )


def compute_rate_summary(frame: pl.DataFrame, *, symbol: str) -> RateSummary:
    """Summarise a series whose values are annual percentage returns.

    The last observation of each calendar year is the year's return. The
    current year is a running estimate, which is exposed separately as
    ``latest_return_pct`` instead of being silently treated as final.
    """

    if frame.is_empty():
        msg = f"no observations available for {symbol}"
        raise ValueError(msg)

    required = {"price_date", "value"}
    if not required.issubset(frame.columns):
        msg = f"observation frame must contain {sorted(required)}"
        raise ValueError(msg)

    data = (
        frame.select(["price_date", "value"])
        .drop_nulls()
        .unique(subset=["price_date"], keep="last")
        .sort("price_date")
    )
    if data.is_empty():
        msg = f"no valid observations available for {symbol}"
        raise ValueError(msg)

    annual = (
        data.with_columns(pl.col("price_date").dt.year().alias("year"))
        .group_by("year", maintain_order=True)
        .agg(pl.col("value").last().alias("rate"))
        .sort("year")
    )
    rates = [float(value) for value in annual["rate"].to_list()]
    years = [int(year) for year in annual["year"].to_list()]

    factors = [1.0 + rate / 100.0 for rate in rates]
    compounded = (math.prod(factors) - 1.0) * 100.0
    average = sum(rates) / len(rates)
    cagr = (math.prod(factors) ** (1.0 / len(rates)) - 1.0) * 100.0 if len(rates) > 1 else None

    std = annual["rate"].std()
    volatility = float(cast(float, std)) if std is not None else None

    best_index = max(range(len(rates)), key=rates.__getitem__)
    worst_index = min(range(len(rates)), key=rates.__getitem__)
    first = data.row(0, named=True)
    last = data.row(-1, named=True)

    return RateSummary(
        symbol=symbol,
        first_date=cast(date, first["price_date"]),
        last_date=cast(date, last["price_date"]),
        observations=data.height,
        years=len(rates),
        latest_year=years[-1],
        latest_return_pct=_round(rates[-1]) or 0.0,
        compounded_return_pct=_round(compounded) or 0.0,
        average_return_pct=_round(average) or 0.0,
        cagr_pct=_round(cagr),
        volatility_pct=_round(volatility),
        best_year=years[best_index],
        best_year_pct=_round(rates[best_index]) or 0.0,
        worst_year=years[worst_index],
        worst_year_pct=_round(rates[worst_index]) or 0.0,
    )
