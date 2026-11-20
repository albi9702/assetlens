"""Tests for the SQLite repository."""

from __future__ import annotations

from datetime import date

import polars as pl
import pytest

from assetlens.db.repository import Repository
from assetlens.errors import InstrumentNotFoundError


def test_upsert_instrument_is_idempotent(repository: Repository, watchlist) -> None:
    first_id = repository.upsert_instrument(watchlist.instruments[0])
    second_id = repository.upsert_instrument(watchlist.instruments[0])

    assert first_id == second_id
    assert repository.list_instruments().height == 1


def test_upsert_prices_is_idempotent(
    repository: Repository, watchlist, price_frame: pl.DataFrame
) -> None:
    instrument_id = repository.upsert_instrument(watchlist.instruments[0])

    first = repository.upsert_prices(instrument_id, price_frame, source="generali")
    second = repository.upsert_prices(instrument_id, price_frame, source="generali")

    assert (first.inserted, first.updated) == (4, 0)
    assert (second.inserted, second.updated) == (0, 4)
    assert repository.count_prices("GI_GEN_TUTELA_CLIMA") == 4
    assert repository.latest_price_date(instrument_id) == date(2024, 1, 5)


def test_get_prices_returns_sorted_frame(
    repository: Repository, watchlist, price_frame: pl.DataFrame
) -> None:
    instrument_id = repository.upsert_instrument(watchlist.instruments[0])
    repository.upsert_prices(instrument_id, price_frame, source="generali")

    frame = repository.get_prices("GI_GEN_TUTELA_CLIMA")
    tail = repository.get_prices("GI_GEN_TUTELA_CLIMA", limit=2)

    assert frame["price_date"].to_list() == sorted(frame["price_date"].to_list())
    assert frame.height == 4
    assert tail.height == 2
    assert tail["price_date"].to_list() == [date(2024, 1, 4), date(2024, 1, 5)]


def test_upsert_prices_rejects_duplicates(
    repository: Repository, watchlist, price_frame: pl.DataFrame
) -> None:
    instrument_id = repository.upsert_instrument(watchlist.instruments[0])
    duplicated = pl.concat([price_frame, price_frame.head(1)])

    with pytest.raises(ValueError, match="duplicate"):
        repository.upsert_prices(instrument_id, duplicated, source="generali")


def test_upsert_prices_rejects_invalid_value(repository: Repository, watchlist) -> None:
    instrument_id = repository.upsert_instrument(watchlist.instruments[0])
    invalid = pl.DataFrame(
        {"price_date": [date(2024, 1, 2)], "value": [0.0]},
        schema={"price_date": pl.Date, "value": pl.Float64},
    )

    with pytest.raises(ValueError, match="prices must be positive"):
        repository.upsert_prices(instrument_id, invalid, source="generali")


def test_missing_instrument_raises(repository: Repository) -> None:
    with pytest.raises(InstrumentNotFoundError):
        repository.get_prices("DOES_NOT_EXIST")


def test_ingestion_run_lifecycle(repository: Repository, watchlist) -> None:
    instrument_id = repository.upsert_instrument(watchlist.instruments[0])
    run_id = repository.start_run(
        provider="generali",
        symbol="GI_GEN_TUTELA_CLIMA",
        instrument_id=instrument_id,
        requested_from=date(2024, 1, 1),
        requested_to=date(2024, 1, 5),
    )
    repository.finish_run(run_id, status="success", rows_fetched=4, rows_inserted=4, rows_updated=0)

    runs = repository.list_runs()
    assert runs.height == 1
    assert runs["status"].to_list() == ["success"]
    assert runs["rows_inserted"].to_list() == [4]


def test_instrument_semantics_are_persisted(repository: Repository, watchlist) -> None:
    instrument = watchlist.instruments[0].model_copy(
        update={
            "asset_class": "separate_account",
            "frequency": "monthly",
            "value_type": "return_pct",
        }
    )

    repository.upsert_instrument(instrument)

    frame = repository.list_instruments()
    assert frame["frequency"].to_list() == ["monthly"]
    assert frame["value_type"].to_list() == ["return_pct"]
    assert frame["asset_class"].to_list() == ["separate_account"]


def test_upsert_prices_accepts_return_series(repository: Repository, watchlist) -> None:
    instrument = watchlist.instruments[0].model_copy(
        update={"frequency": "monthly", "value_type": "return_pct"}
    )
    instrument_id = repository.upsert_instrument(instrument)
    frame = pl.DataFrame(
        {"price_date": [date(2024, 1, 1), date(2024, 2, 1)], "value": [0.0, -2.5]},
        schema={"price_date": pl.Date, "value": pl.Float64},
    )

    stats = repository.upsert_prices(
        instrument_id, frame, source="generali", value_type="return_pct"
    )

    assert stats.inserted == 2
    stored = repository.get_prices("GI_GEN_TUTELA_CLIMA")
    assert stored["value"].to_list() == [0.0, -2.5]


def test_upsert_prices_rejects_unknown_value_type(
    repository: Repository, watchlist, price_frame: pl.DataFrame
) -> None:
    instrument_id = repository.upsert_instrument(watchlist.instruments[0])

    with pytest.raises(ValueError, match="unsupported value_type"):
        repository.upsert_prices(instrument_id, price_frame, source="generali", value_type="yield")
