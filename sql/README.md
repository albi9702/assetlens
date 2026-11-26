# SQL schema

`assetlens_schema.sql` is generated from the Alembic migrations:

```bash
make db-sql
```

It contains the PostgreSQL DDL for the `assetlens` database. The migrations are
the source of truth; change a migration and regenerate the file instead of
editing the SQL by hand.

The file is useful for review, for the Neon SQL Editor and for a manual
bootstrap. Normal deployments use `uv run assetlens init-db`, which runs the
Alembic migrations.
