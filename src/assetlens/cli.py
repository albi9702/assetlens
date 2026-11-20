"""Command-line interface built with Typer and Rich."""

from __future__ import annotations

import json
import os
from collections.abc import Callable
from datetime import date
from functools import wraps
from pathlib import Path
from typing import Annotated, Any

import typer
import uvicorn
from alembic import command
from alembic.config import Config
from rich.console import Console
from rich.table import Table
from sqlalchemy import Engine

from assetlens import __version__
from assetlens.config import (
    Settings,
    Watchlist,
    get_settings,
    load_policy,
    load_watchlist,
    project_root,
)
from assetlens.db.engine import create_all, create_db_engine
from assetlens.db.repository import Repository
from assetlens.errors import AssetLensError
from assetlens.logging import configure_logging
from assetlens.services.analytics import RateSummary, compute_rate_summary, compute_summary
from assetlens.services.ingestion import IngestionService, SyncResult
from assetlens.services.policy import Holding, Premium, compute_policy_summary

console = Console()
app = typer.Typer(
    name="assetlens",
    help="Collect, store and analyse historical prices for funds, ETFs and stocks.",
    no_args_is_help=True,
    add_completion=False,
)
instruments_app = typer.Typer(help="Inspect the watchlist and stored instruments.")
prices_app = typer.Typer(help="Query stored price history.")
analytics_app = typer.Typer(help="Compute performance metrics.")
policy_app = typer.Typer(help="Life-insurance policy snapshots and TIR.")
app.add_typer(instruments_app, name="instruments")
app.add_typer(prices_app, name="prices")
app.add_typer(analytics_app, name="analytics")
app.add_typer(policy_app, name="policy")


class AppState:
    """Lazily initialised resources shared by the CLI commands."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._engine: Engine | None = None
        self._repository: Repository | None = None
        self._watchlist: Watchlist | None = None

    @property
    def engine(self) -> Engine:
        if self._engine is None:
            self._engine = create_db_engine(self.settings.resolved_database_url)
        return self._engine

    @property
    def repository(self) -> Repository:
        if self._repository is None:
            self._repository = Repository(self.engine)
        return self._repository

    @property
    def watchlist(self) -> Watchlist:
        if self._watchlist is None:
            self._watchlist = load_watchlist(self.settings.instruments_file)
        return self._watchlist


def _friendly_errors(func: Callable[..., Any]) -> Callable[..., Any]:
    """Turn domain errors into a short message and exit code 1."""

    @wraps(func)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return func(*args, **kwargs)
        except (AssetLensError, KeyError, ValueError) as exc:
            console.print(f"[red]Error:[/red] {exc}")
            raise typer.Exit(code=1) from exc

    return wrapper


def _state(ctx: typer.Context) -> AppState:
    if not isinstance(ctx.obj, AppState):  # pragma: no cover - defensive
        raise typer.Exit(code=1)
    return ctx.obj


def _parse_date(value: str | None, option: str) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise typer.BadParameter(f"{option} must use the YYYY-MM-DD format, got {value!r}") from exc


def _fmt_date(value: date | None) -> str:
    return value.isoformat() if value is not None else "-"


def _fmt_value(value: float | None, value_type: str = "price") -> str:
    if value is None:
        return "-"
    return f"{value:.2f}%" if value_type == "return_pct" else f"{value:.4f}"


def _upgrade_database(settings: Settings) -> None:
    """Run Alembic migrations, falling back to ``create_all`` without config."""

    root = project_root()
    ini_file = root / "alembic.ini"
    if not ini_file.is_file():
        create_all(create_db_engine(settings.resolved_database_url))
        return

    config = Config(str(ini_file))
    config.set_main_option("script_location", str(root / "migrations"))
    config.set_main_option("sqlalchemy.url", settings.resolved_database_url.replace("%", "%%"))
    command.upgrade(config, "head")


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[
        Path | None,
        typer.Option("--config", "-c", help="Path to the instruments YAML watchlist."),
    ] = None,
    database: Annotated[
        str | None,
        typer.Option("--database", help="SQLAlchemy database URL (defaults to SQLite)."),
    ] = None,
    log_level: Annotated[
        str | None,
        typer.Option("--log-level", help="DEBUG, INFO, WARNING or ERROR."),
    ] = None,
) -> None:
    """Global options shared by every command."""

    settings = get_settings()
    updates: dict[str, Any] = {}
    if config is not None:
        updates["instruments_file"] = config.expanduser().resolve()
    if database is not None:
        updates["database_url"] = database
    if log_level is not None:
        updates["log_level"] = log_level.upper()

    if updates:
        settings = settings.model_copy(update=updates)

    configure_logging(settings.log_level)
    ctx.obj = AppState(settings)


@app.command("init-db")
@_friendly_errors
def init_db(ctx: typer.Context) -> None:
    """Create or migrate the database schema."""

    state = _state(ctx)
    _upgrade_database(state.settings)
    console.print(f"[green]Database ready:[/green] {state.settings.resolved_database_url}")


@app.command()
@_friendly_errors
def sync(
    ctx: typer.Context,
    full: Annotated[
        bool,
        typer.Option("--full", help="Ignore stored data and backfill the full history."),
    ] = False,
    start: Annotated[
        str | None,
        typer.Option("--start", help="Start date (YYYY-MM-DD)."),
    ] = None,
    end: Annotated[
        str | None,
        typer.Option("--end", help="End date (YYYY-MM-DD)."),
    ] = None,
    symbol: Annotated[
        list[str] | None,
        typer.Option("--symbol", "-s", help="Limit the sync to one or more symbols."),
    ] = None,
    provider: Annotated[
        str | None,
        typer.Option("--provider", help="Limit the sync to a single provider."),
    ] = None,
) -> None:
    """Fetch new prices for the selected instruments."""

    state = _state(ctx)
    service = IngestionService(state.repository, state.watchlist, state.settings)
    results = service.sync(
        symbols=symbol,
        provider=provider,
        full=full,
        start=_parse_date(start, "--start"),
        end=_parse_date(end, "--end"),
    )
    _print_sync_results(results)

    if any(result.status == "failed" for result in results):
        raise typer.Exit(code=1)


def _print_sync_results(results: list[SyncResult]) -> None:
    table = Table(title="Sync results")
    table.add_column("Symbol", style="cyan", no_wrap=True)
    table.add_column("Status")
    table.add_column("From")
    table.add_column("To")
    table.add_column("Fetched", justify="right")
    table.add_column("New", justify="right")
    table.add_column("Updated", justify="right")
    table.add_column("Error", style="red")

    for result in results:
        status = "[green]ok[/green]" if result.status == "success" else "[red]failed[/red]"
        table.add_row(
            result.symbol,
            status,
            result.requested_from.isoformat(),
            result.requested_to.isoformat(),
            str(result.fetched),
            str(result.inserted),
            str(result.updated),
            result.error or "",
        )
    console.print(table)


@instruments_app.command("list")
@_friendly_errors
def instruments_list(
    ctx: typer.Context,
    include_inactive: Annotated[
        bool,
        typer.Option("--include-inactive", help="Also show inactive instruments."),
    ] = False,
) -> None:
    """List stored instruments and their coverage."""

    state = _state(ctx)
    frame = state.repository.list_instruments(active_only=not include_inactive)
    if frame.is_empty():
        console.print("No instruments stored yet. Run `assetlens sync` first.")
        return

    table = Table(title="Instruments")
    table.add_column("Symbol", style="cyan", no_wrap=True)
    table.add_column("Name", max_width=28)
    table.add_column("Series")
    table.add_column("Coverage")
    table.add_column("Obs.", justify="right")
    table.add_column("Last value", justify="right")

    for row in frame.to_dicts():
        kind = "return" if row["value_type"] == "return_pct" else row["value_type"]
        coverage = f"{_fmt_date(row['first_date'])} -> {_fmt_date(row['last_date'])}"
        table.add_row(
            row["symbol"],
            row["name"],
            f"{row['frequency']} {kind}",
            coverage,
            str(row["observations"]),
            _fmt_value(row["last_value"], row["value_type"]),
        )
    console.print(table)


@instruments_app.command("runs")
@_friendly_errors
def instruments_runs(
    ctx: typer.Context,
    limit: Annotated[int, typer.Option("--limit", min=1, max=200)] = 20,
) -> None:
    """Show recent ingestion runs."""

    state = _state(ctx)
    frame = state.repository.list_runs(limit=limit)
    if frame.is_empty():
        console.print("No ingestion runs recorded yet.")
        return

    table = Table(title=f"Last {frame.height} ingestion runs")
    table.add_column("Started")
    table.add_column("Symbol", style="cyan", no_wrap=True)
    table.add_column("Status")
    table.add_column("From")
    table.add_column("To")
    table.add_column("Fetched", justify="right")
    table.add_column("New", justify="right")
    table.add_column("Updated", justify="right")
    table.add_column("Error", style="red")

    for row in frame.to_dicts():
        table.add_row(
            row["started_at"].isoformat(timespec="seconds") if row["started_at"] else "-",
            row["symbol"],
            row["status"],
            _fmt_date(row["requested_from"]),
            _fmt_date(row["requested_to"]),
            str(row["rows_fetched"]),
            str(row["rows_inserted"]),
            str(row["rows_updated"]),
            row["error"] or "",
        )
    console.print(table)


@prices_app.command("show")
@_friendly_errors
def prices_show(
    ctx: typer.Context,
    symbol: Annotated[str, typer.Argument(help="Instrument symbol, e.g. GI_GEN_TUTELA_CLIMA.")],
    provider: Annotated[
        str | None, typer.Option("--provider", help="Disambiguate symbols across providers.")
    ] = None,
    start: Annotated[str | None, typer.Option("--start", help="Start date (YYYY-MM-DD).")] = None,
    end: Annotated[str | None, typer.Option("--end", help="End date (YYYY-MM-DD).")] = None,
    tail: Annotated[
        int,
        typer.Option("--tail", min=0, help="Show only the last N rows; 0 means all."),
    ] = 20,
    output_format: Annotated[
        str,
        typer.Option("--format", "-f", help="Output format: table, csv or json."),
    ] = "table",
) -> None:
    """Show stored prices for an instrument."""

    state = _state(ctx)
    instrument = state.repository.get_instrument(symbol, provider)
    frame = state.repository.get_prices(
        symbol,
        provider=provider,
        start=_parse_date(start, "--start"),
        end=_parse_date(end, "--end"),
        limit=tail or None,
    )
    if frame.is_empty():
        console.print(f"[yellow]No prices stored for {symbol}.[/yellow]")
        raise typer.Exit(code=1)

    fmt = output_format.lower()
    if fmt == "csv":
        typer.echo(frame.write_csv())
        return
    if fmt == "json":
        typer.echo(frame.write_json())
        return
    if fmt != "table":
        raise typer.BadParameter("--format must be one of: table, csv, json")

    kind = "return" if instrument.value_type == "return_pct" else instrument.value_type
    table = Table(title=f"{symbol} {kind} series ({frame.height} rows)")
    table.add_column("Date")
    table.add_column("Value", justify="right")
    table.add_column("Source")
    for row in frame.to_dicts():
        value = _fmt_value(row["value"], instrument.value_type)
        table.add_row(row["price_date"].isoformat(), value, row["source"])
    console.print(table)


@analytics_app.command("summary")
@_friendly_errors
def analytics_summary(
    ctx: typer.Context,
    symbol: Annotated[str, typer.Argument(help="Instrument symbol.")],
    provider: Annotated[
        str | None, typer.Option("--provider", help="Disambiguate symbols across providers.")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON instead of a table.")] = False,
) -> None:
    """Compute performance metrics for an instrument."""

    state = _state(ctx)
    frame = state.repository.get_prices(symbol, provider=provider)
    if frame.is_empty():
        console.print(f"[yellow]No prices stored for {symbol}.[/yellow]")
        raise typer.Exit(code=1)

    instrument = state.repository.get_instrument(symbol, provider)
    if instrument.value_type == "return_pct":
        rate_summary = compute_rate_summary(frame, symbol=symbol)
        if as_json:
            typer.echo(json.dumps(rate_summary.as_dict(), indent=2, default=str))
            return
        _print_rate_summary(rate_summary)
        return

    summary = compute_summary(frame, symbol=symbol)
    if as_json:
        typer.echo(json.dumps(summary.as_dict(), indent=2, default=str))
        return

    table = Table(title=f"{symbol} performance")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    rows: list[tuple[str, str]] = [
        ("First date", summary.first_date.isoformat()),
        ("Last date", summary.last_date.isoformat()),
        ("Observations", str(summary.observations)),
        ("First value", f"{summary.first_value:.6f}"),
        ("Last value", f"{summary.last_value:.6f}"),
        ("Total return", f"{summary.total_return_pct:+.2f}%"),
        ("CAGR", _fmt_pct(summary.cagr_pct)),
        ("Volatility (ann.)", _fmt_pct(summary.volatility_pct)),
        ("Max drawdown", _fmt_pct(summary.max_drawdown_pct)),
        ("Return 1M", _fmt_pct(summary.return_1m_pct)),
        ("Return 3M", _fmt_pct(summary.return_3m_pct)),
        ("Return 1Y", _fmt_pct(summary.return_1y_pct)),
        ("Return YTD", _fmt_pct(summary.return_ytd_pct)),
    ]
    for label, value in rows:
        table.add_row(label, value)
    console.print(table)


def _fmt_pct(value: float | None) -> str:
    return f"{value:+.2f}%" if value is not None else "-"


def _print_rate_summary(summary: RateSummary) -> None:
    table = Table(title=f"{summary.symbol} return series")
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    rows: list[tuple[str, str]] = [
        ("First date", summary.first_date.isoformat()),
        ("Last date", summary.last_date.isoformat()),
        ("Observations", str(summary.observations)),
        ("Annual returns", str(summary.years)),
        ("Latest return", f"{summary.latest_year}: {_fmt_pct(summary.latest_return_pct)}"),
        ("Compounded return", _fmt_pct(summary.compounded_return_pct)),
        ("Average annual return", _fmt_pct(summary.average_return_pct)),
        ("CAGR", _fmt_pct(summary.cagr_pct)),
        ("Volatility (annual)", _fmt_pct(summary.volatility_pct)),
        (
            "Best year",
            f"{summary.best_year}: {_fmt_pct(summary.best_year_pct)}",
        ),
        (
            "Worst year",
            f"{summary.worst_year}: {_fmt_pct(summary.worst_year_pct)}",
        ),
    ]
    for label, value in rows:
        table.add_row(label, value)
    console.print(table)


@app.command()
@_friendly_errors
def export(
    ctx: typer.Context,
    symbol: Annotated[str, typer.Argument(help="Instrument symbol.")],
    output: Annotated[Path, typer.Option("--output", "-o", help="Destination file.")],
    provider: Annotated[str | None, typer.Option("--provider")] = None,
    start: Annotated[str | None, typer.Option("--start", help="Start date (YYYY-MM-DD).")] = None,
    end: Annotated[str | None, typer.Option("--end", help="End date (YYYY-MM-DD).")] = None,
) -> None:
    """Export the full price history to CSV, JSON or Parquet."""

    state = _state(ctx)
    frame = state.repository.get_prices(
        symbol,
        provider=provider,
        start=_parse_date(start, "--start"),
        end=_parse_date(end, "--end"),
    )
    if frame.is_empty():
        console.print(f"[yellow]No prices stored for {symbol}.[/yellow]")
        raise typer.Exit(code=1)

    output = output.expanduser()
    output.parent.mkdir(parents=True, exist_ok=True)
    suffix = output.suffix.lower()
    if suffix == ".csv":
        frame.write_csv(output)
    elif suffix == ".parquet":
        frame.write_parquet(output)
    elif suffix == ".json":
        frame.write_json(output)
    else:
        raise typer.BadParameter("output extension must be .csv, .json or .parquet")

    console.print(f"[green]Exported {frame.height} rows to[/green] {output}")


@policy_app.command("compute")
@_friendly_errors
def policy_compute(
    ctx: typer.Context,
    policy_file: Annotated[
        Path | None,
        typer.Option(
            "--policy",
            help="Policy YAML (default: ASSETLENS_POLICY_FILE or config/policy.yaml).",
        ),
    ] = None,
) -> None:
    """Compute a snapshot from the policy file and store it in SQLite."""

    state = _state(ctx)
    path = (policy_file or state.settings.policy_file).expanduser().resolve()
    policy = load_policy(path)

    summary = compute_policy_summary(
        [Premium(premium.paid_on, premium.amount) for premium in policy.premiums],
        [Holding(h.name, h.value, h.quote, h.nav) for h in policy.holdings],
        as_of=policy.as_of,
        currency=policy.currency,
        tax_rate=policy.tax_rate,
    )
    state.repository.upsert_policy_snapshot(summary)
    _print_policy("Policy snapshot", summary)
    console.print(
        f"[green]Stored in[/green] {state.settings.resolved_database_url} "
        "[green]table[/green] policy_snapshots"
    )


@policy_app.command("show")
@_friendly_errors
def policy_show(
    ctx: typer.Context,
    as_of: Annotated[
        str | None, typer.Option("--as-of", help="Snapshot date (YYYY-MM-DD).")
    ] = None,
    as_json: Annotated[bool, typer.Option("--json", help="Print JSON instead of tables.")] = False,
) -> None:
    """Show a stored snapshot (latest by default)."""

    state = _state(ctx)
    snapshot = state.repository.get_policy_snapshot(_parse_date(as_of, "--as-of"))
    if as_json:
        typer.echo(json.dumps(_policy_dict(snapshot), indent=2, default=str))
        return
    _print_policy("Policy snapshot", snapshot)


@policy_app.command("list")
@_friendly_errors
def policy_list(ctx: typer.Context) -> None:
    """List stored policy snapshots."""

    state = _state(ctx)
    frame = state.repository.list_policy_snapshots()
    if frame.is_empty():
        console.print("No policy snapshots yet. Run `assetlens policy compute`.")
        return

    table = Table(title="Policy snapshots")
    table.add_column("As of", no_wrap=True)
    table.add_column("Premiums", justify="right")
    table.add_column("Gross", justify="right")
    table.add_column("Gain", justify="right")
    table.add_column("Tax", justify="right")
    table.add_column("Net", justify="right")
    table.add_column("TIR gross", justify="right")
    table.add_column("TIR net", justify="right")
    for row in frame.to_dicts():
        table.add_row(
            row["as_of"].isoformat(),
            f"{row['total_premiums']:,.2f}",
            f"{row['gross_value']:,.2f}",
            f"{row['capital_gain']:+,.2f}",
            f"{row['tax_amount']:,.2f}",
            f"{row['net_value']:,.2f}",
            _fmt_pct(row["gross_irr_pct"]),
            _fmt_pct(row["net_irr_pct"]),
        )
    console.print(table)


def _print_policy(title: str, snapshot: Any) -> None:
    table = Table(title=title)
    table.add_column("Metric")
    table.add_column("Value", justify="right")
    rows: list[tuple[str, str]] = [
        ("As of", snapshot.as_of.isoformat()),
        ("Currency", snapshot.currency),
        ("Premiums paid", f"{snapshot.total_premiums:,.2f}"),
        ("GESAV", f"{snapshot.gesav_value:,.2f}"),
        ("Funds", f"{snapshot.funds_value:,.2f}"),
        ("Gross value", f"{snapshot.gross_value:,.2f}"),
        ("Capital gain", f"{snapshot.capital_gain:+,.2f}"),
        ("Tax rate", f"{snapshot.tax_rate:.2%}"),
        ("Tax", f"{snapshot.tax_amount:,.2f}"),
        ("Net value", f"{snapshot.net_value:,.2f}"),
        ("TIR gross", _fmt_pct(snapshot.gross_irr_pct)),
        ("TIR net", _fmt_pct(snapshot.net_irr_pct)),
    ]
    for label, value in rows:
        table.add_row(label, value)
    console.print(table)

    holdings_table = Table(title="Holdings")
    holdings_table.add_column("Name", max_width=32)
    holdings_table.add_column("Quotes", justify="right")
    holdings_table.add_column("NAV", justify="right")
    holdings_table.add_column("Value", justify="right")
    for holding in snapshot.holdings:
        if isinstance(holding, dict):
            name = holding["name"]
            quote = holding["quote"]
            nav = holding["nav"]
            value = holding["value"]
        else:
            name = holding.name
            quote = holding.quote
            nav = holding.nav
            value = holding.value
        holdings_table.add_row(
            name,
            f"{quote:.5f}" if quote is not None else "-",
            f"{nav:.2f}" if nav is not None else "-",
            f"{value:,.2f}",
        )
    console.print(holdings_table)


def _policy_dict(snapshot: Any) -> dict[str, Any]:
    return {
        "as_of": snapshot.as_of,
        "currency": snapshot.currency,
        "total_premiums": snapshot.total_premiums,
        "gesav_value": snapshot.gesav_value,
        "funds_value": snapshot.funds_value,
        "gross_value": snapshot.gross_value,
        "capital_gain": snapshot.capital_gain,
        "tax_rate": snapshot.tax_rate,
        "tax_amount": snapshot.tax_amount,
        "net_value": snapshot.net_value,
        "gross_irr_pct": snapshot.gross_irr_pct,
        "net_irr_pct": snapshot.net_irr_pct,
        "premiums": snapshot.premiums,
        "holdings": snapshot.holdings,
    }


@app.command()
def api(
    ctx: typer.Context,
    host: Annotated[str, typer.Option("--host")] = "127.0.0.1",
    port: Annotated[int, typer.Option("--port", min=1, max=65535)] = 8000,
    reload: Annotated[bool, typer.Option("--reload", help="Auto-reload on code changes.")] = False,
) -> None:
    """Start the read-only FastAPI server."""

    settings = _state(ctx).settings
    # Propagate CLI overrides to the reloader subprocess.
    os.environ["ASSETLENS_DATABASE_URL"] = settings.resolved_database_url
    os.environ["ASSETLENS_INSTRUMENTS_FILE"] = str(settings.instruments_file)
    os.environ["ASSETLENS_LOG_LEVEL"] = settings.log_level

    uvicorn.run(
        "assetlens.api.app:create_app",
        factory=True,
        host=host,
        port=port,
        reload=reload,
        log_level=settings.log_level.lower(),
    )


@app.command("db")
def db_info(ctx: typer.Context) -> None:
    """Print the database location and basic statistics."""

    state = _state(ctx)
    settings = state.settings
    console.print(f"URL:  {settings.resolved_database_url}")
    if settings.database_path is not None:
        console.print(f"Path: {settings.database_path}")
        console.print(f"Exists: {settings.database_path.exists()}")


@app.command()
def version() -> None:
    """Print the assetlens version."""

    console.print(f"assetlens {__version__}")


def run() -> None:  # pragma: no cover - console-script entry point
    app()


if __name__ == "__main__":  # pragma: no cover
    app()
