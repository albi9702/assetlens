# Development

## Prerequisites

- Python 3.14 (uv can install it automatically)
- [uv](https://docs.astral.sh/uv/)
- SQLite CLI (optional, useful for inspection)
- VS Code (recommended)

## Setup

```bash
git clone <repository-url> assetlens
cd assetlens
uv sync --all-groups
cp .env.example .env        # optional
uv run pre-commit install   # optional but recommended
```

`uv sync` creates `.venv`, installs runtime and development dependencies and
installs the `assetlens` console script in editable mode.

The committed `config/policy.yaml` is synthetic example data. For real figures,
copy it to `config/policy.local.yaml` (gitignored) and set
`ASSETLENS_POLICY_FILE=config/policy.local.yaml` in `.env`. Both the local file
and `.env` stay out of commits; the CI and scheduled jobs never read them.

SQLite remains the default. To develop against PostgreSQL or Neon, follow
[Neon setup](neon.md) and set `ASSETLENS_DATABASE_URL` plus, for migrations,
`ASSETLENS_MIGRATION_DATABASE_URL` in `.env`.

## Everyday commands

| Task | Command |
| --- | --- |
| Run the CLI | `uv run assetlens --help` |
| Create/migrate the DB | `uv run assetlens init-db` |
| Full backfill | `uv run assetlens sync --full` |
| Incremental sync | `uv run assetlens sync` |
| Run tests | `uv run pytest` |
| Coverage | `uv run pytest --cov --cov-report=term-missing` |
| Lint | `uv run ruff check src tests` |
| Format | `uv run ruff format src tests` |
| Type check | `uv run mypy src` |
| All gates | `make check` |
| API with reload | `uv run assetlens api --reload` |
| Dashboard | <http://127.0.0.1:8000/dashboard/> |
| SQLite shell | `sqlite3 data/assetlens.db` |

The same commands are available through `make` (`make install`, `make test`,
`make lint`, `make api`, `make db-shell`).

## Automation

`.github/workflows/data-daily.yml` runs the incremental sync every day at 04:17
UTC; `data-monthly.yml` rebuilds the full history at 05:23 UTC on the first of
the month. Both are market-only: they never compute or store a policy snapshot,
so they cannot leak private figures. Both are also manually dispatchable and
write to Neon through the `ASSETLENS_DATABASE_URL` secret, using the direct
`ASSETLENS_MIGRATION_DATABASE_URL` secret for `init-db`. There is no database
cache or artifact. Their `push` trigger is limited to the workflow file itself
and exists only to register them in the Actions tab on first push; normal
commits do not start a sync. See the "Scheduled ingestion" section of
`README.md` and [Neon setup](neon.md).

## Project conventions

- Code, identifiers, docstrings and documentation are in English.
- Type hints everywhere; `mypy` must pass.
- `ruff` owns linting and formatting (line length 100).
- Public functions have docstrings explaining *why*, not just *what*.
- New behaviour comes with tests; provider tests never hit the network.
- Domain errors derive from `AssetLensError`.

## Debugging in VS Code

Open the repository root, not `src/`. The included `.vscode/settings.json`
points the Python extension at `.venv/bin/python` and enables pytest discovery.

Available launch configurations:

| Configuration | What it does |
| --- | --- |
| `CLI: sync (full)` | Runs `assetlens sync --full` with the debugger attached. |
| `CLI: sync (incremental)` | Runs `assetlens sync`. |
| `CLI: analytics summary` | Runs the summary for `GI_GEN_TUTELA_CLIMA`. |
| `API: uvicorn` | Starts the API on port 8000 without reload, breakpoints enabled. |
| `Tests: current file` | Runs the test file open in the editor. |

Breakpoints work in `src/assetlens/**`. For the API, set a breakpoint in a route
and call it with `curl` or the `/docs` page.

Useful debug settings already applied: `justMyCode: false` and
`PYTHONUNBUFFERED=1`, so provider internals and logs are visible.

## Testing strategy

- `tests/test_generali.py` - .NET date parsing, payload validation, GESAV
  rate series and HTTP behaviour with `respx` (no real network).
- `tests/test_repository.py` - idempotent upserts, duplicate detection, run
  audit.
- `tests/test_ingestion.py` - full and incremental windows, failure isolation.
- `tests/test_analytics.py` - price metrics plus compounded return series.
- `tests/test_api.py` - FastAPI routes against a temporary SQLite database.
- `tests/test_config.py` - watchlist validation.
- `tests/test_cli.py` - CLI smoke tests with `CliRunner`.

Fixtures live in `tests/fixtures/`. Add a small recorded payload when you add a
provider.

## Adding an instrument

1. Append an entry to `config/instruments.yaml`:

```yaml
  - provider: generali
    symbol: GI_GEN_NEW_FUND
    name: Generali New Fund
    asset_class: fund
    frequency: daily
    value_type: price
    currency: EUR
```

2. Run `uv run assetlens sync --symbol GI_GEN_NEW_FUND`.
3. Verify with `uv run assetlens instruments list`.

No code change is required for an existing provider. For a monthly return
series, set `frequency: monthly`, `value_type: return_pct` and, when the history
predates the global backfill start, `backfill_start`. GESAV is the reference
example.

## Adding a provider

See [providers.md](providers.md). The short version: implement
`BaseProvider.fetch_history`, register the class, add a watchlist entry, add a
`respx` test.

## Database migrations

```bash
uv run alembic revision --autogenerate -m "describe change"
uv run alembic upgrade head
```

Review autogenerated migrations: SQLite requires batch mode for some `ALTER`
operations, which `migrations/env.py` already enables.

## Troubleshooting

### `database is locked`

A long write transaction is open, usually a second sync. Wait for it to finish
or stop the other process. WAL mode and a 10-second busy timeout handle normal
CLI/API concurrency.

### The API starts but returns no instruments

Run `uv run assetlens init-db` and `uv run assetlens sync --full` first. Check
`uv run assetlens db` to confirm which database file the API is using.

### GESAV values look like prices

They are not. `value_type: return_pct` means the stored number is an annual
percentage (for example `3.13` = `+3.13%`). Use `assetlens analytics summary
GESAV` for the compounded view; `prices show GESAV` prints the raw rates with a
`%` suffix.

### The latest quote is a few days old

The source publishes with a delay (for example, on 2026-11-20 the latest quote
was 2026-11-16). This is expected; the next sync picks it up.

### `uv sync` cannot find Python 3.14

```bash
uv python install 3.14
uv sync --all-groups
```

### Git identity is not configured

Before the first commit:

```bash
git config user.name "Your Name"
git config user.email "you@example.com"
```

Use a GitHub noreply address if you prefer not to publish your email.
