"""Daily reconstruction of a policy from the stored price history.

The snapshot is the anchor; the ``prices`` table is what changes every day.
Each premium is treated as a separate tranche:

* it starts at exactly the amount paid on its payment date;
* it moves with a market index rebuilt from the snapshot holdings (NAV ratios
  for the funds, daily-compounded published returns for GESAV);
* it is calibrated so that the sum of all tranches on the snapshot date is the
  snapshot's gross value, distributed with the same weights used for the
  premium attribution (the annual rate is the policy TIR).

This avoids the error of applying today's whole portfolio to past dates, which
would show a 20.000 EUR value at a time when only 5.000 EUR had been paid.

The result is an *indicative* series: the composition is that of the snapshot,
so the UI labels it as a reconstruction rather than an official valuation.
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import date, timedelta

from assetlens.services.policy import DAYS_PER_YEAR, DEFAULT_TAX_RATE, Holding, Premium

MIN_MARKET_MOVE = 0.05


@dataclass(frozen=True, slots=True)
class DailyPolicyPoint:
    """One reconstructed day of the policy."""

    date: date
    invested: float
    gesav_value: float
    funds_value: float
    gross_value: float
    net_value: float


def normalize_name(name: str) -> str:
    """Return a comparison key that ignores case, accents and punctuation."""

    decomposed = unicodedata.normalize("NFKD", name)
    return "".join(
        char for char in decomposed if not unicodedata.combining(char) and char.isalnum()
    ).lower()


STOP_WORDS = {"del", "della", "dei", "delle", "degli", "al", "alla", "ai", "alle", "e", "di"}


def normalize_policy_name(name: str) -> str:
    """Reduce a holding name or a provider symbol to its compartment key."""

    decomposed = unicodedata.normalize("NFKD", name)
    plain = "".join(char for char in decomposed if not unicodedata.combining(char)).lower()
    words = re.findall(r"[a-z0-9]+", plain)
    key = "".join(word for word in words if word not in STOP_WORDS)
    for prefix in ("generali", "gigen"):
        if key.startswith(prefix):
            return key[len(prefix) :]
    return key


def _days(start: date, end: date) -> Iterator[date]:
    current = start
    while current <= end:
        yield current
        current += timedelta(days=1)


def _build_lookup(
    prices: Mapping[str, Sequence[tuple[date, float]]],
) -> dict[str, tuple[list[date], list[float]]]:
    lookup: dict[str, tuple[list[date], list[float]]] = {}
    for symbol, observations in prices.items():
        ordered = sorted(observations)
        if ordered:
            lookup[symbol] = ([day for day, _ in ordered], [value for _, value in ordered])
    return lookup


def _value_at(
    lookup: Mapping[str, tuple[list[date], list[float]]], symbol: str, day: date
) -> float | None:
    """Last known value on or before ``day``, or the first known value."""

    if symbol not in lookup:
        return None
    days, values = lookup[symbol]
    index = bisect_right(days, day) - 1
    return values[max(index, 0)]


@dataclass(frozen=True, slots=True)
class _Market:
    """Portfolio index rebuilt from the snapshot holdings."""

    lookup: Mapping[str, tuple[list[date], list[float]]]
    gesav_symbol: str
    gesav_index: Mapping[date, float]
    gesav_anchor: float
    fund_holdings: tuple[tuple[Holding, str | None, float], ...]
    gesav_weight: float
    weight_total: float

    def parts(self, day: date) -> tuple[float, float]:
        """GESAV and fund components of the portfolio index on ``day``."""

        gesav_part = self.gesav_weight * self.gesav_index[day] / self.gesav_anchor
        fund_part = 0.0
        for holding, symbol, anchor in self.fund_holdings:
            current = _value_at(self.lookup, symbol, day) if symbol is not None else None
            fund_part += holding.value * (current / anchor if current else 1.0)
        return gesav_part, fund_part

    def index(self, day: date) -> float:
        gesav_part, fund_part = self.parts(day)
        return (gesav_part + fund_part) / self.weight_total


def _build_market(
    *,
    holdings: Sequence[Holding],
    prices: Mapping[str, Sequence[tuple[date, float]]],
    gesav_symbol: str,
    origin: date,
    as_of: date,
    end: date,
) -> _Market:
    lookup = _build_lookup(prices)
    symbols_by_name = {normalize_policy_name(symbol): symbol for symbol in prices}
    fund_holdings: list[tuple[Holding, str | None, float]] = []
    for holding in holdings:
        if holding.is_separate_account:
            continue
        symbol = symbols_by_name.get(normalize_policy_name(holding.name))
        anchor = _value_at(lookup, symbol, as_of) if symbol is not None else None
        fund_holdings.append((holding, symbol, anchor or 1.0))

    gesav_index: dict[date, float] = {}
    index = 1.0
    for day in _days(origin, end):
        rate = _value_at(lookup, gesav_symbol, day) or 0.0
        index *= (1.0 + rate / 100.0) ** (1.0 / DAYS_PER_YEAR)
        gesav_index[day] = index

    weight_total = sum(holding.value for holding in holdings)
    return _Market(
        lookup=lookup,
        gesav_symbol=gesav_symbol,
        gesav_index=gesav_index,
        gesav_anchor=gesav_index[as_of] or 1.0,
        fund_holdings=tuple(fund_holdings),
        gesav_weight=sum(h.value for h in holdings if h.is_separate_account),
        weight_total=weight_total or 1.0,
    )


@dataclass(frozen=True, slots=True)
class _Tranche:
    premium: Premium
    target: float
    start_index: float
    duration: int

    def value_at(self, day: date, as_of: date, market: _Market) -> float:
        """Value of this tranche on ``day``."""

        if day >= as_of:
            # After the snapshot there is no new composition information: the
            # tranche simply grows with the reconstructed market index.
            return self.target * market.index(day)
        if self.duration <= 0:
            return self.premium.amount

        denominator = 1.0 - self.start_index
        if abs(denominator) < MIN_MARKET_MOVE:
            # The market barely moved between payment and snapshot: a ratio
            # would explode, so interpolate over elapsed days instead.
            progress = (day - self.premium.paid_on).days / self.duration
        else:
            progress = (market.index(day) - self.start_index) / denominator
        progress = min(max(progress, 0.0), 1.0)
        return self.premium.amount + (self.target - self.premium.amount) * progress


def _build_tranches(
    premiums: Sequence[Premium],
    *,
    as_of: date,
    gross_anchor: float,
    annual_rate: float,
    market: _Market,
) -> tuple[_Tranche, ...]:
    """One tranche per premium, calibrated to the snapshot value."""

    total_premiums = sum(premium.amount for premium in premiums)
    raw_weights = [
        premium.amount * (1.0 + annual_rate) ** ((as_of - premium.paid_on).days / DAYS_PER_YEAR)
        for premium in premiums
    ]
    raw_total = sum(raw_weights)
    if raw_total > 0:
        weights = [value / raw_total for value in raw_weights]
    elif total_premiums > 0:
        weights = [premium.amount / total_premiums for premium in premiums]
    else:
        weights = []

    return tuple(
        _Tranche(
            premium=premium,
            target=premium.amount + (gross_anchor - total_premiums) * weight,
            start_index=market.index(premium.paid_on),
            duration=(as_of - premium.paid_on).days,
        )
        for premium, weight in zip(premiums, weights, strict=True)
    )


def reconstruct_daily_policy(
    *,
    as_of: date,
    premiums: Sequence[Premium],
    holdings: Sequence[Holding],
    prices: Mapping[str, Sequence[tuple[date, float]]],
    gesav_symbol: str,
    start: date,
    end: date,
    tax_rate: float = DEFAULT_TAX_RATE,
    annual_rate: float = 0.0,
) -> tuple[DailyPolicyPoint, ...]:
    """Replay the stored prices to build one policy value per day.

    ``start`` is the first returned day; ``end`` must not precede the snapshot
    date, which is the anchor of the reconstruction. ``annual_rate`` is the
    gross TIR, used to distribute the snapshot gain across the premiums.
    """

    if start > end:
        raise ValueError("start cannot be after end")
    if end < as_of:
        raise ValueError("end cannot be before the snapshot date")
    if not 0.0 <= tax_rate <= 1.0:
        raise ValueError("tax_rate must be between 0 and 1")

    ordered_premiums = tuple(sorted(premiums, key=lambda premium: premium.paid_on))
    gross_anchor = sum(holding.value for holding in holdings)
    origin = min([start, as_of, *(p.paid_on for p in ordered_premiums)])
    market = _build_market(
        holdings=holdings,
        prices=prices,
        gesav_symbol=gesav_symbol,
        origin=origin,
        as_of=as_of,
        end=end,
    )
    tranches = _build_tranches(
        ordered_premiums,
        as_of=as_of,
        gross_anchor=gross_anchor,
        annual_rate=annual_rate,
        market=market,
    )

    points: list[DailyPolicyPoint] = []
    for day in _days(start, end):
        invested = 0.0
        gross = 0.0
        for tranche in tranches:
            if day < tranche.premium.paid_on:
                continue
            invested += tranche.premium.amount
            gross += tranche.value_at(day, as_of, market)

        gesav_part, fund_part = market.parts(day)
        part_total = gesav_part + fund_part
        gesav_value = gross * gesav_part / part_total if part_total > 0 else 0.0
        tax = max(gross - invested, 0.0) * tax_rate
        points.append(
            DailyPolicyPoint(
                date=day,
                invested=round(invested, 2),
                gesav_value=round(gesav_value, 2),
                funds_value=round(gross - gesav_value, 2),
                gross_value=round(gross, 2),
                net_value=round(gross - tax, 2),
            )
        )

    return tuple(points)
