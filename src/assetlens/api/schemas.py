"""Pydantic schemas exposed by the HTTP API."""

from __future__ import annotations

from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class InstrumentOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    provider: str
    symbol: str
    name: str
    asset_class: str
    frequency: str
    value_type: str
    currency: str
    isin: str | None = None
    active: bool = True
    first_date: date | None = None
    last_date: date | None = None
    observations: int = 0
    last_value: float | None = None


class InstrumentDetailOut(InstrumentOut):
    url: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class PriceOut(BaseModel):
    price_date: date
    value: float
    source: str
    ingested_at: datetime | None = None


class SummaryOut(BaseModel):
    value_type: Literal["price"] = "price"
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


class RateSummaryOut(BaseModel):
    value_type: Literal["return_pct"] = "return_pct"
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


class HealthOut(BaseModel):
    status: str
    database: str
    instruments: int
    prices: int


class PremiumOut(BaseModel):
    paid_on: date
    amount: float


class HoldingOut(BaseModel):
    name: str
    quote: float | None = None
    nav: float | None = None
    value: float


class PolicySnapshotSummaryOut(BaseModel):
    """Scalar snapshot columns, used for the time-series charts."""

    as_of: date
    currency: str
    total_premiums: float
    gesav_value: float
    funds_value: float
    gross_value: float
    capital_gain: float
    tax_rate: float
    tax_amount: float
    net_value: float
    gross_irr_pct: float | None = None
    net_irr_pct: float | None = None


class PolicySnapshotOut(PolicySnapshotSummaryOut):
    model_config = ConfigDict(from_attributes=True)

    id: int
    premiums: list[PremiumOut] = Field(default_factory=list)
    holdings: list[HoldingOut] = Field(default_factory=list)


class PremiumTrancheOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    paid_on: date
    amount: float
    years_invested: float
    weight_pct: float
    projected_value: float
    gain: float
    value_weight_pct: float


class PolicyAnalysisOut(BaseModel):
    snapshot: PolicySnapshotOut
    gross_tranches: list[PremiumTrancheOut]
    net_tranches: list[PremiumTrancheOut]


class DailyPolicyPointOut(BaseModel):
    date: date
    invested: float
    gesav_value: float
    funds_value: float
    gross_value: float
    net_value: float


class MarketPointOut(BaseModel):
    date: date
    value: float


class FundSeriesOut(BaseModel):
    symbol: str
    name: str
    points: list[MarketPointOut]


class MarketHistoryOut(BaseModel):
    """Raw NAV/return observations, without any euro conversion."""

    range: str
    reference_date: date | None = None
    funds: list[FundSeriesOut]
    gesav: list[MarketPointOut]
    premium_dates: list[date]


class FundPremiumComparisonOut(BaseModel):
    """NAV at a premium date versus the latest NAV."""

    paid_on: date
    nav_at_premium: float
    nav_date: date
    latest_nav: float
    latest_date: date
    change_pct: float


class FundPerformanceOut(BaseModel):
    """One fund and its comparison for every premium date."""

    symbol: str
    name: str
    comparisons: list[FundPremiumComparisonOut]


class FundPerformanceReportOut(BaseModel):
    """Fund comparisons for the whole policy, GESAV excluded."""

    reference_date: date | None = None
    premiums: list[PremiumOut]
    funds: list[FundPerformanceOut]
