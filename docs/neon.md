# Neon / PostgreSQL setup

The project still runs on SQLite by default. This page describes the online
setup used for the home deployment: one Neon project, two databases.

## Project layout

```text
Neon project: assets
└── branch: main (shown as "production" in the Console)
    ├── database: assetlens   ← this application
    ├── database: spese       ← future personal expenses app
    └── branch: dev           ← optional copy for tests and migrations
```

`neondb` is the default database created by Neon and is not used.

The two databases are deliberately separate:

- separate connection strings and credentials;
- separate `alembic_version` tables;
- no accidental cross-database joins;
- no `search_path` issues with PgBouncer.

The future `spese` project owns its own schema and migrations. This repository
does not create tables there.

## Create the databases

Run these statements once in the Neon SQL Editor, connected to `neondb`:

```sql
CREATE DATABASE assetlens OWNER neondb_owner;
CREATE DATABASE spese OWNER neondb_owner;
```

Neon does not allow `CREATE DATABASE` inside a transaction. Run each statement
on its own.

## Connection strings

Neon exposes two useful endpoints:

| Use | Host | Secret |
| --- | --- | --- |
| Application, `sync`, API | `ep-...-pooler...` | `ASSETLENS_DATABASE_URL` |
| `init-db`, Alembic, `pg_dump` | `ep-...` (no `-pooler`) | `ASSETLENS_MIGRATION_DATABASE_URL` |

Both must point to the `assetlens` database and include `sslmode=require`.
SQLAlchemy URLs must use the psycopg 3 driver:

```text
postgresql+psycopg://USER:PASSWORD@HOST/assetlens?sslmode=require
```

Plain `postgresql://` URLs are normalized automatically, so copying Neon's
string is also fine.

Why the split: Neon's pooled endpoint is PgBouncer in transaction mode. Session
settings such as `SET search_path`, temporary tables and SQL-level prepared
statements are not reliable there. Alembic and `pg_dump` should use the direct
endpoint.

## Local setup

Add the secrets to the gitignored `.env`:

```bash
ASSETLENS_POLICY_FILE=config/policy.local.yaml
ASSETLENS_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST-pooler/assetlens?sslmode=require
ASSETLENS_MIGRATION_DATABASE_URL=postgresql+psycopg://USER:PASSWORD@HOST/assetlens?sslmode=require
```

Then:

```bash
uv sync --all-groups
uv run assetlens db          # password is redacted
uv run assetlens init-db     # direct connection
uv run assetlens sync --full # pooled connection
uv run assetlens policy compute
uv run assetlens api
```

`assetlens db` and `/health` never print the password.

## GitHub Actions

Add the same two values as repository secrets:

- `ASSETLENS_DATABASE_URL` (pooled)
- `ASSETLENS_MIGRATION_DATABASE_URL` (direct)

The daily and monthly workflows run `init-db` with the direct URL and `sync`
with the pooled URL. They fail early if a secret is missing. No database cache
or artifact is used: Neon is the single source of truth.

## SQL schema for `init-db`

Alembic remains the source of truth. The PostgreSQL DDL is generated from the
migrations and committed for review or manual use:

```bash
make db-sql
# writes sql/assetlens_schema.sql
```

The generated file is safe to paste into the Neon SQL Editor when you want to
inspect or bootstrap the schema without running Python. Do not edit it by hand:
change the migration and regenerate.

## Branches and free-plan limits

The free plan includes 10 branches, 1 GB of Postgres storage per project,
100 CU-hours per project per month and 5 GB of egress per project. Compute
scales to zero after five minutes of inactivity. The current dataset is a few
thousand rows, so it fits comfortably.

Use the root branch for real data and create a child `dev` branch before a
migration or a risky change. A child branch is a copy-on-write clone, including
both databases. Delete it when the test is finished.

## Reloading or moving data

The source is the public Generali endpoint, so there is no SQLite dump to
migrate. Point the application at Neon and run:

```bash
uv run assetlens init-db
uv run assetlens sync --full
uv run assetlens policy compute
```

The upserts are idempotent, so the same commands can be repeated safely.

## Security notes

- Never commit `.env`, `config/policy.local.yaml` or a connection string.
- The API is read-only, but if you expose it publicly add authentication: the
  policy snapshot contains personal figures.
- If a connection string was pasted into a chat, issue or log, rotate the role
  password in Neon and update the local `.env` and GitHub Secrets.
