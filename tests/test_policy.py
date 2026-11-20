"""Tests for the policy analytics and the policy_snapshots table."""

from __future__ import annotations

from datetime import date

import pytest

from assetlens.config import PolicyConfig
from assetlens.db.repository import Repository
from assetlens.errors import PolicySnapshotNotFoundError
from assetlens.services.policy import (
    Holding,
    PolicySummary,
    Premium,
    compute_policy_summary,
    compute_premium_tranches,
    xirr,
)

AS_OF = date(2024, 1, 1)


def _summary() -> PolicySummary:
    """Build the same synthetic snapshot stored in ``config/policy.yaml``."""

    premiums = [Premium(date(year, 1, 1), 1000.00) for year in range(2020, 2025)]
    holdings = [
        Holding("GESAV", 3000.00),
        Holding("Generali Salute e Benessere", 400.00, 100.00000, 4.00),
        Holding("Generali Crescita Sostenibile", 450.00, 100.00000, 4.50),
        Holding("Generali Consumo Responsabile", 500.00, 100.00000, 5.00),
        Holding("Generali Pari Opportunità", 550.00, 100.00000, 5.50),
        Holding("Generali Tutela del Clima", 600.00, 100.00000, 6.00),
    ]
    return compute_policy_summary(premiums, holdings, as_of=AS_OF)


def test_xirr_matches_a_simple_one_year_return() -> None:
    rate = xirr([(date(2020, 1, 1), -1000.0), (date(2021, 1, 1), 1100.0)])

    assert rate is not None
    assert rate == pytest.approx(0.0998, abs=1e-3)


def test_xirr_returns_none_without_a_sign_change() -> None:
    assert xirr([(date(2020, 1, 1), -1000.0), (date(2021, 1, 1), -100.0)]) is None
    assert xirr([]) is None


def test_policy_summary_computes_tax_and_net() -> None:
    summary = _summary()

    assert summary.total_premiums == pytest.approx(5000.00)
    assert summary.gesav_value == pytest.approx(3000.00)
    assert summary.funds_value == pytest.approx(2500.00)
    assert summary.gross_value == pytest.approx(5500.00)
    assert summary.capital_gain == pytest.approx(500.00)
    assert summary.tax_amount == pytest.approx(100.00)
    assert summary.net_value == pytest.approx(5400.00)
    assert summary.gross_irr_pct == pytest.approx(4.769205, abs=1e-6)
    assert summary.net_irr_pct == pytest.approx(3.850551, abs=1e-6)
    assert summary.net_irr_pct < summary.gross_irr_pct


def test_policy_summary_does_not_tax_a_loss() -> None:
    summary = compute_policy_summary(
        [Premium(date(2024, 1, 1), 1000.00)],
        [Holding("GESAV", 900.00)],
        as_of=date(2024, 12, 31),
    )

    assert summary.capital_gain == pytest.approx(-100.00)
    assert summary.tax_amount == 0.0
    assert summary.net_value == pytest.approx(900.00)
    assert summary.gross_irr_pct is not None
    assert summary.gross_irr_pct < 0


def test_policy_summary_rejects_inconsistent_input() -> None:
    with pytest.raises(ValueError, match="as_of"):
        compute_policy_summary(
            [Premium(date(2024, 1, 1), 1000.00)],
            [Holding("GESAV", 900.00)],
            as_of=date(2023, 1, 1),
        )

    with pytest.raises(ValueError, match="tax_rate"):
        compute_policy_summary(
            [Premium(date(2024, 1, 1), 1000.00)],
            [Holding("GESAV", 900.00)],
            as_of=date(2024, 12, 31),
            tax_rate=1.5,
        )


def test_policy_config_rejects_as_of_before_last_premium() -> None:
    with pytest.raises(ValueError, match="as_of"):
        PolicyConfig.model_validate(
            {
                "as_of": "2023-01-01",
                "premiums": [{"paid_on": "2024-01-01", "amount": 1000.0}],
                "holdings": [{"name": "GESAV", "value": 900.0}],
            }
        )


def test_premium_tranches_attribute_the_gross_value() -> None:
    summary = _summary()
    tranches = compute_premium_tranches(
        summary.premiums,
        as_of=AS_OF,
        annual_rate=(summary.gross_irr_pct or 0.0) / 100.0,
    )

    assert [tranche.paid_on for tranche in tranches] == [
        premium.paid_on for premium in summary.premiums
    ]
    assert sum(tranche.amount for tranche in tranches) == pytest.approx(summary.total_premiums)
    assert sum(tranche.projected_value for tranche in tranches) == pytest.approx(
        summary.gross_value, abs=0.1
    )
    assert sum(tranche.weight_pct for tranche in tranches) == pytest.approx(100.0, abs=1e-3)
    assert sum(tranche.value_weight_pct for tranche in tranches) == pytest.approx(100.0, abs=1e-3)
    # The last premium is paid on the valuation date, so it has no growth yet.
    assert all(tranche.projected_value > tranche.amount for tranche in tranches[:-1])
    assert tranches[-1].projected_value == pytest.approx(tranches[-1].amount)


def test_premium_tranches_attribute_the_net_value() -> None:
    summary = _summary()
    tranches = compute_premium_tranches(
        summary.premiums,
        as_of=AS_OF,
        annual_rate=(summary.net_irr_pct or 0.0) / 100.0,
    )

    assert sum(tranche.projected_value for tranche in tranches) == pytest.approx(
        summary.net_value, abs=0.1
    )
    assert all(tranche.value_weight_pct > 0 for tranche in tranches)


def test_premium_tranches_reject_invalid_input() -> None:
    with pytest.raises(ValueError, match="annual_rate"):
        compute_premium_tranches(
            [Premium(date(2024, 1, 1), 1000.00)],
            as_of=date(2024, 12, 31),
            annual_rate=-1.0,
        )

    with pytest.raises(ValueError, match="as_of"):
        compute_premium_tranches(
            [Premium(date(2024, 1, 1), 1000.00)],
            as_of=date(2023, 1, 1),
            annual_rate=0.01,
        )


def test_policy_snapshot_roundtrip(repository: Repository) -> None:
    summary = _summary()

    snapshot_id = repository.upsert_policy_snapshot(summary)
    stored = repository.get_policy_snapshot(AS_OF)
    latest = repository.get_policy_snapshot()
    frame = repository.list_policy_snapshots()

    assert stored.id == snapshot_id
    assert stored.as_of == AS_OF
    assert stored.net_value == pytest.approx(summary.net_value)
    assert stored.gross_irr_pct == pytest.approx(summary.gross_irr_pct or 0.0)
    assert stored.premiums[0]["amount"] == pytest.approx(1000.00)
    assert latest.id == snapshot_id
    assert frame.height == 1
    assert frame["as_of"].to_list() == [AS_OF]


def test_policy_snapshot_upsert_is_idempotent(repository: Repository) -> None:
    first = repository.upsert_policy_snapshot(_summary())
    second = repository.upsert_policy_snapshot(_summary())

    assert first == second
    assert repository.list_policy_snapshots().height == 1


def test_missing_policy_snapshot_raises(repository: Repository) -> None:
    with pytest.raises(PolicySnapshotNotFoundError):
        repository.get_policy_snapshot()
