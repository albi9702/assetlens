# assetlens

`assetlens` collects, stores and analyses historical prices for funds, ETFs and
stocks. It ships with a provider for the Generali asset-management quotes and a
watchlist containing the five compartments of **Genera Sviluppo Sostenibile**
plus **GESAV**, a separate account that publishes annual return rates rather
than a NAV.

The project is intentionally provider-agnostic: adding a new data source means
writing a small adapter, while storage, CLI, analytics and API stay unchanged.

## Features

- **Incremental and full backfills** with idempotent upserts.
- **Price and return series**: instruments declare their `value_type`, so the
  same storage serves funds, ETFs, stocks and separate accounts.
- **SQLite locally and PostgreSQL/Neon online**, with dialect-aware upserts.
- **Polars** for every analytical computation (no pandas).
- **Provider abstraction** with throttling, retries and typed errors.
- **CLI** built with Typer and Rich.
- **Read-only FastAPI** backend with OpenAPI docs.
- **Audit trail** of every ingestion run in `ingestion_runs`.
- **Tested** with pytest, `respx` and recorded fixtures.

## Quickstart

Requirements: Python 3.14 and [uv](https://docs.astral.sh/uv/).

```bash
git clone <repository-url> assetlens
cd assetlens

uv sync --all-groups
uv run assetlens init-db
uv run assetlens sync --full

uv run assetlens instruments list
uv run assetlens analytics summary GI_GEN_TUTELA_CLIMA
uv run assetlens analytics summary GESAV
uv run assetlens api --reload
```

The API is then available at <http://127.0.0.1:8000> and its interactive
documentation at <http://127.0.0.1:8000/docs>.

The database is created at `data/assetlens.db`:

```bash
sqlite3 data/assetlens.db "select count(*) from prices;"
sqlite3 data/assetlens.db "select * from instruments;"
```

To use PostgreSQL or Neon instead, see [Neon setup](docs/neon.md). The local
SQLite path remains the default, so the quickstart works offline.

## CLI reference

| Command | Description |
| --- | --- |
| `assetlens init-db` | Create or migrate the database schema (Alembic). |
| `assetlens sync` | Incremental sync with a configurable overlap. |
| `assetlens sync --full` | Backfill from the instrument start or `ASSETLENS_BACKFILL_START`. |
| `assetlens sync --symbol GI_GEN_TUTELA_CLIMA` | Sync a single instrument. |
| `assetlens instruments list` | Coverage per instrument (series, dates, rows). |
| `assetlens instruments runs` | Recent ingestion runs and errors. |
| `assetlens prices show SYMBOL --tail 30` | Print stored observations. |
| `assetlens prices show SYMBOL -f csv` | Print observations as CSV. |
| `assetlens analytics summary SYMBOL` | Price metrics or compounded annual returns. |
| `assetlens policy compute` | Compute the policy snapshot from the configured policy YAML and store it. |
| `assetlens policy show` | Show the latest policy snapshot (gross, tax, net, TIR). |
| `assetlens policy list` | List stored policy snapshots. |
| `assetlens export SYMBOL -o out.parquet` | Export to CSV, JSON or Parquet. |
| `assetlens api --reload` | Start the read-only API. |
| `assetlens db` | Print database URL, path and existence. |

Global options: `--config PATH`, `--database URL`, `--log-level LEVEL`.

## API

| Endpoint | Description |
| --- | --- |
| `GET /health` | Database status and row counts. |
| `GET /instruments` | Instruments with coverage, series type and last value. |
| `GET /instruments/{symbol}` | Instrument details, including metadata. |
| `GET /instruments/{symbol}/prices` | Observations with `start`, `end`, `limit`. |
| `GET /instruments/{symbol}/summary` | Price summary or return-series summary. |

The API is read-only on purpose: ingestion is an explicit CLI operation, so the
MVP does not expose unauthenticated write endpoints.

## Scheduled ingestion (GitHub Actions)

Two workflows keep the online PostgreSQL/Neon database up to date. Both can also
be started manually from the Actions tab (`workflow_dispatch`).

| Workflow | Schedule (UTC) | What it does |
| --- | --- | --- |
| `data-daily.yml` | every day at 04:17 | `assetlens sync` from the last stored date (delta, market only) |
| `data-monthly.yml` | 1st of the month at 05:23 | `assetlens sync --full` (idempotent full refresh, market only) |

The database is **not** committed and there is no database cache or artifact
anymore: all data lives in Neon. The monthly job re-fetches the full market
history and upserts it, which heals drift or missing days without deleting the
database. The daily job only fetches the new observations.

The workflows need two repository secrets:

| Secret | Used by | Connection |
| --- | --- | --- |
| `ASSETLENS_DATABASE_URL` | `sync`, `instruments list` | Pooled Neon URL (`-pooler`) |
| `ASSETLENS_MIGRATION_DATABASE_URL` | `init-db`/Alembic | Direct Neon URL (no `-pooler`) |

Both URLs must include `sslmode=require`. The jobs fail early with a clear
message if either secret is missing.

**The scheduled jobs never write policy snapshots.** They only sync the public
market series. Policy data is computed locally from your gitignored
`config/policy.local.yaml`.

The Generali endpoint is public, but the database credentials are not: keep them
in GitHub Secrets only. The workflows raise `ASSETLENS_MAX_RETRIES` to 5 and the
request delay to 1 second to tolerate temporary network errors. Edit the `cron`
value in the workflow file to change the time; cron is always UTC. Scheduled
workflows only run from the default branch (currently `public`). Each workflow
also has a `push` trigger limited to its own file: GitHub only shows workflows in
the Actions tab after their first run, so that trigger registers them when the
workflow file is pushed. Ordinary code pushes do not start a sync.

## Configuration

Settings are read from environment variables prefixed with `ASSETLENS_` or from
a local `.env` file. See `.env.example` for every option.

The database is chosen with `ASSETLENS_DATABASE_URL`. When it is unset, the
project uses SQLite at `data/assetlens.db`. For PostgreSQL/Neon:

```bash
ASSETLENS_DATABASE_URL=postgresql+psycopg://user:pass@host-pooler/db?sslmode=require
ASSETLENS_MIGRATION_DATABASE_URL=postgresql+psycopg://user:pass@host/db?sslmode=require
```

`ASSETLENS_MIGRATION_DATABASE_URL` is optional and defaults to the application
URL. Set it to the direct Neon endpoint so `assetlens init-db` does not run
Alembic through PgBouncer. Plain `postgresql://` URLs are normalized to the
psycopg 3 driver automatically.

The watchlist lives in `config/instruments.yaml`:

```yaml
providers:
  generali:
    base_url: https://asset.generali.it

instruments:
  - provider: generali
    symbol: GI_GEN_TUTELA_CLIMA
    name: Generali Tutela del Clima
    asset_class: fund
    frequency: daily        # daily, weekly, monthly, quarterly, yearly
    value_type: price       # price or return_pct
    currency: EUR

  - provider: generali
    symbol: GESAV
    name: GESAV - Gestione Separata
    asset_class: separate_account
    frequency: monthly
    value_type: return_pct
    backfill_start: 1981-01-01
```

`frequency` widens the incremental overlap automatically (40 days for monthly
series, 100 for quarterly, 370 for yearly). `backfill_start` overrides the
global start for instruments with a longer history. To track another fund, add
an entry and run `assetlens sync`.

## Tracked instruments

| Symbol | Series | Coverage | Observations |
| --- | --- | --- | --- |
| `GI_GEN_TUTELA_CLIMA` | daily price | 2019-11-15 → 2026-11-16 | 1,761 |
| `GI_GEN_CONSUMO_RESPONSABILE` | daily price | 2019-11-15 → 2026-11-16 | 1,761 |
| `GI_GEN_PARI_OPPORTUNITA` | daily price | 2019-11-15 → 2026-11-16 | 1,761 |
| `GI_GEN_SALUTE_BENESSERE` | daily price | 2019-11-15 → 2026-11-16 | 1,761 |
| `GI_GEN_CRESCITA_SOSTENIBILE` | daily price | 2019-11-15 → 2026-11-16 | 1,761 |
| `GESAV` | monthly annual return | 1981-01-01 → 2026-11-01 | 349 |

The current database holds 9,154 observations. GESAV values are percentages
(for example `3.13` means `+3.13%`), not euro prices.

## Policy snapshot

The committed `config/policy.yaml` contains **example data only**: five yearly
premiums of 1,000, a separate account worth 3,000 and five funds worth 2,500 in
total. It exists so a fresh clone can try the commands without exposing anyone's
real figures. The YAML describes the premium schedule, the GESAV and unit-linked
holdings, the valuation date and the substitute tax rate. The command computes
the gross value, the capital gain, the 20% tax, the net value and the
money-weighted return (TIR/XIRR), then stores one self-contained row in
`policy_snapshots`:

```bash
uv run assetlens policy compute
uv run assetlens policy show
uv run assetlens policy list
```

To use your own policy, copy the example to `config/policy.local.yaml`, edit it
and add this line to `.env` (the file and the environment variable are both
gitignored):

```bash
ASSETLENS_POLICY_FILE=config/policy.local.yaml
```

Then the same commands read `config/policy.local.yaml`. Never commit that file
or an exported database containing your snapshot.

```sql
SELECT as_of, gross_value, capital_gain, tax_amount, net_value,
       gross_irr_pct, net_irr_pct
FROM policy_snapshots;
```

The TIR is the annual rate that zeroes the NPV of the dated cash flows
(premiums out, current value in), using a 365.25-day year. A loss has no tax,
so net value equals gross value.

## Dashboard

The dashboard reads the same database through the existing FastAPI app. It
is a single static page with plain JavaScript and a vendored Chart.js: no Node,
no build step and no external requests, so it works offline at home.

```bash
uv run assetlens api
# open http://127.0.0.1:8000/  (redirects to /dashboard/)
```

Available at:

| URL | Content |
| --- | --- |
| `/dashboard/` | The dashboard page. |
| `/policy/snapshots` | Stored snapshots, oldest first (time series). |
| `/policy/analysis` | Latest snapshot plus the value attributed to each premium. |
| `/policy/daily?start=YYYY-MM-DD` | Indicative day-by-day reconstruction from the stored prices. |
| `/policy/fund-performance` | Each unit-linked fund's NAV at every premium date versus the latest NAV; GESAV excluded. |
| `/market/history?range=...` | Raw fund NAVs and GESAV return; `1y`, `6m`, `3m`, `ytd`, `premiums` or `all`. |

The "Fondi ai giorni dei premi" card compares every unit-linked fund at each
premium date with its latest NAV: a grouped bar chart shows the percentage
change and a colour-coded table shows the NAV then, the NAV now and the
percentage. GESAV is excluded because it is a separate account, not a quoted
fund.

The charts follow a few simple rules: euro axes on bars start at zero, values
are shown with compact axis ticks and exact tooltips, categories are labelled
with their percentage, colours are consistent between charts and colour-blind
safe, no pie charts and no smoothed lines. The daily line chart starts its euro
axis above zero (with a note) so small day-to-day movements stay visible; hover
over it to read the exact amount of every series on that date. Every bar chart
shows its exact amount next to the bar and has an accompanying data table. Both
date pickers stay usable with a single snapshot.

The "value per premium" view uses the TIR to attribute the current value to
each payment. For premium `i`, with `r` the gross or net TIR:

```
years_i       = (as_of - paid_on_i) / 365.25
value_i       = premium_i * (1 + r)^years_i
share_i       = premium_i / sum(premiums)
value_share_i = value_i / current_value
```

Because `r` solves `sum(value_i) = current_value`, the attributed values add up
to the displayed total. With the example file the first premium is 20.0% of the
capital paid in and 21.9% of the current gross value.

Two further line charts show values only, not euro amounts: one line per
unit-linked fund (NAV per quote) and, below it, the GESAV published annual
return in percent. Both use the same period picker: last year, 6 months,
3 months, year to date, only the months in which a premium was paid, or the
whole history. The GESAV series is never mixed into the fund chart.

The daily view starts each premium at exactly the amount paid, then moves it
with a market index rebuilt from the snapshot holdings (fund NAV ratios and the
published GESAV return). The gain on the snapshot date is distributed with the
same TIR weights, so the series equals the invested capital on the first premium
date and matches the stored snapshot exactly on its valuation date. Values after
that date are a projection of the same composition.

## Project layout

```
src/assetlens/
├── config.py            # settings and watchlist models
├── cli.py               # Typer commands
├── db/                  # SQLAlchemy models, engine, repository
├── providers/           # BaseProvider, registry, Generali adapter
├── services/            # ingestion orchestration, Polars analytics, policy/TIR
├── api/                 # FastAPI factory, routes, schemas
└── web/                 # static dashboard (HTML/CSS/JS + vendored Chart.js)
```

## Documentation

- [Architecture](docs/architecture.md)
- [Data model](docs/data-model.md)
- [Providers](docs/providers.md)
- [Neon / PostgreSQL setup](docs/neon.md)
- [Development](docs/development.md)

## Data source and responsible use

The Generali adapter calls the same JSON endpoint used by the public charts on
<https://asset.generali.it/quotazioni/genera-sviluppo-sostenibile> and
<https://asset.generali.it/quotazioni/gestioni-separate>. Requests are throttled
by default (0.5 s between calls), retried with exponential backoff and identified
by a custom User-Agent.

The data belongs to the respective provider. This project is meant for personal
analysis; do not redistribute the collected data without permission. Always
check the provider's terms of service before increasing request rates.

## Roadmap

- Additional providers (Yahoo Finance/Stooq for ETFs and stocks, ECB for FX).
- Portfolio transactions and positions with P&L.
- Scheduled sync (cron or systemd timer).
- PostgreSQL support for a hosted deployment.
- A small dashboard on top of the read-only API.

## License

MIT. See [LICENSE](LICENSE).
