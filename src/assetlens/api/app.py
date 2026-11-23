"""FastAPI application factory and routes.

The API is intentionally read-only in the MVP: ingestion happens through the
CLI, which avoids exposing a write endpoint without authentication.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict
from datetime import date, timedelta
from pathlib import Path
from typing import Annotated, Literal

import polars as pl
from fastapi import APIRouter, Depends, FastAPI, HTTPException, Query, Request
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from assetlens import __version__
from assetlens.api.schemas import (
    DailyPolicyPointOut,
    FundSeriesOut,
    HealthOut,
    InstrumentDetailOut,
    InstrumentOut,
    MarketHistoryOut,
    MarketPointOut,
    PolicyAnalysisOut,
    PolicySnapshotOut,
    PolicySnapshotSummaryOut,
    PremiumTrancheOut,
    PriceOut,
    RateSummaryOut,
    SummaryOut,
)
from assetlens.config import Settings, get_settings
from assetlens.db.engine import create_db_engine
from assetlens.db.repository import Repository
from assetlens.errors import InstrumentNotFoundError, PolicySnapshotNotFoundError
from assetlens.services.analytics import compute_rate_summary, compute_summary
from assetlens.services.policy import Holding, Premium, compute_premium_tranches
from assetlens.services.policy_history import normalize_name, reconstruct_daily_policy

WEB_DIR = Path(__file__).resolve().parent.parent / "web"

router = APIRouter()


def get_repository(request: Request) -> Repository:
    return request.app.state.repository


@router.get("/health", response_model=HealthOut, tags=["system"])
def health(request: Request) -> HealthOut:
    repository: Repository = request.app.state.repository
    settings: Settings = request.app.state.settings
    try:
        instruments = repository.list_instruments().height
        prices = repository.count_prices()
    except Exception as exc:
        raise HTTPException(status_code=503, detail=f"database unavailable: {exc}") from exc
    return HealthOut(
        status="ok",
        database=settings.redacted_database_url,
        instruments=instruments,
        prices=prices,
    )


@router.get("/instruments", response_model=list[InstrumentOut], tags=["instruments"])
def list_instruments(
    repository: Annotated[Repository, Depends(get_repository)],
    include_inactive: Annotated[bool, Query()] = False,
) -> list[InstrumentOut]:
    frame = repository.list_instruments(active_only=not include_inactive)
    return [InstrumentOut.model_validate(row) for row in frame.to_dicts()]


@router.get(
    "/instruments/{symbol}",
    response_model=InstrumentDetailOut,
    tags=["instruments"],
)
def get_instrument(
    repository: Annotated[Repository, Depends(get_repository)],
    symbol: str,
    provider: Annotated[str | None, Query()] = None,
) -> InstrumentDetailOut:
    frame = repository.list_instruments()
    rows = frame.filter(pl.col("symbol") == symbol)
    if provider is not None:
        rows = rows.filter(pl.col("provider") == provider)
    if rows.is_empty():
        raise HTTPException(status_code=404, detail=f"instrument not found: {symbol}")

    record = rows.to_dicts()[0]
    instrument = repository.get_instrument(symbol, provider)
    return InstrumentDetailOut.model_validate(
        {**record, "url": instrument.url, "metadata": instrument.meta}
    )


@router.get(
    "/instruments/{symbol}/prices",
    response_model=list[PriceOut],
    tags=["prices"],
)
def get_prices(  # noqa: PLR0917
    repository: Annotated[Repository, Depends(get_repository)],
    symbol: str,
    provider: Annotated[str | None, Query()] = None,
    start: Annotated[date | None, Query()] = None,
    end: Annotated[date | None, Query()] = None,
    limit: Annotated[int | None, Query(ge=1, le=20_000)] = None,
) -> list[PriceOut]:
    frame = repository.get_prices(symbol, provider=provider, start=start, end=end, limit=limit)
    return [PriceOut.model_validate(row) for row in frame.to_dicts()]


@router.get(
    "/instruments/{symbol}/summary",
    response_model=SummaryOut | RateSummaryOut,
    tags=["analytics"],
)
def get_summary(
    repository: Annotated[Repository, Depends(get_repository)],
    symbol: str,
    provider: Annotated[str | None, Query()] = None,
) -> SummaryOut | RateSummaryOut:
    frame = repository.get_prices(symbol, provider=provider)
    if frame.is_empty():
        raise HTTPException(status_code=404, detail=f"no observations stored for {symbol}")
    instrument = repository.get_instrument(symbol, provider)
    if instrument.value_type == "return_pct":
        rate_summary = compute_rate_summary(frame, symbol=symbol)
        return RateSummaryOut.model_validate(rate_summary.as_dict())
    summary = compute_summary(frame, symbol=symbol)
    return SummaryOut.model_validate(summary.as_dict())


@router.get(
    "/policy/snapshots",
    response_model=list[PolicySnapshotSummaryOut],
    tags=["policy"],
)
def list_policy_snapshots(
    repository: Annotated[Repository, Depends(get_repository)],
) -> list[PolicySnapshotSummaryOut]:
    """Return the stored snapshots, oldest first, for the time-series charts."""

    frame = repository.list_policy_snapshots().sort("as_of")
    return [PolicySnapshotSummaryOut.model_validate(row) for row in frame.to_dicts()]


@router.get(
    "/policy/analysis",
    response_model=PolicyAnalysisOut,
    tags=["policy"],
)
def get_policy_analysis(
    repository: Annotated[Repository, Depends(get_repository)],
    as_of: Annotated[
        date | None, Query(description="Snapshot date; defaults to the latest.")
    ] = None,
) -> PolicyAnalysisOut:
    """Return a snapshot plus the value attributed to each premium."""

    snapshot = repository.get_policy_snapshot(as_of)
    premiums = [
        Premium(paid_on=date.fromisoformat(item["paid_on"]), amount=item["amount"])
        for item in snapshot.premiums
    ]
    gross_pct = snapshot.gross_irr_pct if snapshot.gross_irr_pct is not None else 0.0
    net_pct = snapshot.net_irr_pct if snapshot.net_irr_pct is not None else gross_pct
    gross_tranches = compute_premium_tranches(
        premiums, as_of=snapshot.as_of, annual_rate=gross_pct / 100.0
    )
    net_tranches = compute_premium_tranches(
        premiums, as_of=snapshot.as_of, annual_rate=net_pct / 100.0
    )
    return PolicyAnalysisOut(
        snapshot=PolicySnapshotOut.model_validate(snapshot),
        gross_tranches=[PremiumTrancheOut.model_validate(tranche) for tranche in gross_tranches],
        net_tranches=[PremiumTrancheOut.model_validate(tranche) for tranche in net_tranches],
    )


@router.get(
    "/policy/daily",
    response_model=list[DailyPolicyPointOut],
    tags=["policy"],
)
def get_policy_daily(
    repository: Annotated[Repository, Depends(get_repository)],
    start: Annotated[date | None, Query(description="First day of the daily series.")] = None,
) -> list[DailyPolicyPointOut]:
    """Rebuild the policy value day by day from the stored price history."""

    snapshot = repository.get_policy_snapshot()
    first_day = start or snapshot.as_of
    premiums = [
        Premium(paid_on=date.fromisoformat(item["paid_on"]), amount=item["amount"])
        for item in snapshot.premiums
    ]
    # Prices are needed from the first premium to calibrate each tranche, even
    # when the chart starts at the snapshot date.
    earliest = min([first_day, snapshot.as_of, *(p.paid_on for p in premiums)])
    price_start = earliest - timedelta(days=30)
    instruments = repository.list_instruments(active_only=False).to_dicts()

    prices: dict[str, list[tuple[date, float]]] = {}
    last_day = snapshot.as_of
    for instrument in instruments:
        frame = repository.get_prices(
            instrument["symbol"], provider=instrument["provider"], start=price_start
        )
        if frame.is_empty():
            continue
        prices[instrument["symbol"]] = list(
            zip(
                frame["price_date"].to_list(),
                frame["value"].to_list(),
                strict=True,
            )
        )
        last_price = frame["price_date"].max()
        if isinstance(last_price, date):
            last_day = max(last_day, last_price)

    gesav_symbol = next(
        (
            row["symbol"]
            for row in instruments
            if row["value_type"] == "return_pct" and normalize_name(row["name"]).startswith("gesav")
        ),
        None,
    )
    if gesav_symbol is None:
        raise HTTPException(status_code=404, detail="GESAV return series is not stored")

    holdings = [
        Holding(name=item["name"], value=item["value"], quote=item["quote"], nav=item["nav"])
        for item in snapshot.holdings
    ]
    gross_pct = snapshot.gross_irr_pct if snapshot.gross_irr_pct is not None else 0.0
    points = reconstruct_daily_policy(
        as_of=snapshot.as_of,
        premiums=premiums,
        holdings=holdings,
        prices=prices,
        gesav_symbol=gesav_symbol,
        start=first_day,
        end=last_day,
        tax_rate=snapshot.tax_rate,
        annual_rate=gross_pct / 100.0,
    )
    return [DailyPolicyPointOut.model_validate(asdict(point)) for point in points]


MarketRange = Literal["1y", "6m", "3m", "ytd", "premiums", "all"]


def _range_filter(
    period: MarketRange,
    reference: date,
    premium_dates: Sequence[date],
) -> tuple[date | None, set[tuple[int, int]] | None]:
    """Return the start date or the premium months selected by ``period``."""

    if period == "1y":
        return reference - timedelta(days=365), None
    if period == "6m":
        return reference - timedelta(days=183), None
    if period == "3m":
        return reference - timedelta(days=91), None
    if period == "ytd":
        return date(reference.year, 1, 1), None
    if period == "premiums":
        return None, {(day.year, day.month) for day in premium_dates}
    return None, None


@router.get(
    "/market/history",
    response_model=MarketHistoryOut,
    tags=["market"],
)
def get_market_history(
    repository: Annotated[Repository, Depends(get_repository)],
    period: Annotated[
        MarketRange, Query(alias="range", description="Window of the raw observations.")
    ] = "1y",
) -> MarketHistoryOut:
    """Return fund NAVs and the GESAV return, without any euro conversion."""

    instruments = repository.list_instruments(active_only=False).to_dicts()
    funds = [
        row for row in instruments if row["value_type"] == "price" and row["asset_class"] == "fund"
    ]
    gesav_rows = [
        row
        for row in instruments
        if row["value_type"] == "return_pct" and normalize_name(row["name"]).startswith("gesav")
    ]

    series: dict[str, list[tuple[date, float]]] = {}
    for row in [*funds, *gesav_rows]:
        frame = repository.get_prices(row["symbol"], provider=row["provider"])
        if frame.is_empty():
            continue
        series[row["symbol"]] = list(
            zip(
                frame["price_date"].to_list(),
                frame["value"].to_list(),
                strict=True,
            )
        )

    reference = max((day for points in series.values() for day, _ in points), default=None)
    premium_dates: list[date] = []
    if period == "premiums":
        snapshot = repository.get_policy_snapshot()
        premium_dates = [date.fromisoformat(item["paid_on"]) for item in snapshot.premiums]
    start, months = (
        _range_filter(period, reference, premium_dates) if reference is not None else (None, None)
    )

    def selected(point: tuple[date, float]) -> bool:
        day = point[0]
        if months is not None:
            return (day.year, day.month) in months
        return start is None or day >= start

    fund_series = [
        FundSeriesOut(
            symbol=row["symbol"],
            name=row["name"],
            points=[
                MarketPointOut(date=day, value=value)
                for day, value in series.get(row["symbol"], [])
                if selected((day, value))
            ],
        )
        for row in funds
    ]
    gesav = [
        MarketPointOut(date=day, value=value)
        for row in gesav_rows
        for day, value in series.get(row["symbol"], [])
        if selected((day, value))
    ]
    return MarketHistoryOut(
        range=period,
        reference_date=reference,
        funds=fund_series,
        gesav=gesav,
        premium_dates=premium_dates,
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the FastAPI application.

    The factory form makes the app easy to test (inject settings) and to run
    with ``uvicorn --reload``.
    """

    resolved_settings = settings or get_settings()
    engine = create_db_engine(resolved_settings.resolved_database_url)
    repository = Repository(engine)

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        yield
        engine.dispose()

    app = FastAPI(
        title="assetlens API",
        description=(
            "Read-only API over the assetlens SQLite store. "
            "Prices are ingested with the `assetlens sync` CLI command."
        ),
        version=__version__,
        lifespan=lifespan,
    )
    app.state.settings = resolved_settings
    app.state.repository = repository
    app.state.engine = engine
    app.include_router(router)

    if WEB_DIR.is_dir():
        app.mount("/dashboard", StaticFiles(directory=WEB_DIR, html=True), name="dashboard")

        @app.get("/", include_in_schema=False)
        def dashboard_redirect() -> RedirectResponse:
            return RedirectResponse(url="/dashboard/")

    @app.exception_handler(InstrumentNotFoundError)
    async def _instrument_not_found(
        _request: Request, exc: InstrumentNotFoundError
    ) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    @app.exception_handler(PolicySnapshotNotFoundError)
    async def _policy_not_found(
        _request: Request, exc: PolicySnapshotNotFoundError
    ) -> JSONResponse:
        return JSONResponse(status_code=404, content={"detail": str(exc)})

    return app
