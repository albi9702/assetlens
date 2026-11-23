"""Application settings and watchlist loading.

Settings come from environment variables (prefix ``ASSETLENS_``) or a local
``.env`` file. The watchlist is a YAML document that describes providers and
the instruments to track. Adding a new instrument never requires code changes:
add an entry to ``config/instruments.yaml`` and run ``assetlens sync``.
"""

from __future__ import annotations

import os
from datetime import date
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

from assetlens import __version__

AssetClass = Literal["fund", "etf", "stock", "crypto", "separate_account", "other"]
Frequency = Literal["daily", "weekly", "monthly", "quarterly", "yearly"]
ValueType = Literal["price", "return_pct"]


def project_root() -> Path:
    """Return the repository root, falling back to the current directory.

    The lookup is based on the presence of both ``pyproject.toml`` and
    ``alembic.ini``. It can be overridden with ``ASSETLENS_ROOT``, which is
    useful when the package is installed outside the source tree.
    """

    override = os.getenv("ASSETLENS_ROOT")
    if override:
        return Path(override).expanduser().resolve()

    for parent in Path(__file__).resolve().parents:
        if (parent / "pyproject.toml").is_file() and (parent / "alembic.ini").is_file():
            return parent

    return Path.cwd()


class InstrumentConfig(BaseModel):
    """A single instrument declared in the watchlist."""

    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1)
    symbol: str = Field(min_length=1)
    name: str = Field(min_length=1)
    asset_class: AssetClass = "fund"
    frequency: Frequency = "daily"
    value_type: ValueType = "price"
    currency: str = Field(default="EUR", min_length=3, max_length=3)
    isin: str | None = Field(default=None, min_length=12, max_length=12)
    url: str | None = None
    active: bool = True
    backfill_start: date | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("provider", "symbol", "name", "currency")
    @classmethod
    def _strip(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            msg = "value cannot be blank"
            raise ValueError(msg)
        return stripped

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, value: str) -> str:
        return value.upper()


class ProviderConfig(BaseModel):
    """Provider-level HTTP configuration declared in the watchlist."""

    model_config = ConfigDict(extra="forbid")

    base_url: str = Field(min_length=1)
    timeout_seconds: float | None = Field(default=None, gt=0)
    request_delay_seconds: float | None = Field(default=None, ge=0)
    max_retries: int | None = Field(default=None, ge=1, le=10)


class Watchlist(BaseModel):
    """Validated content of ``config/instruments.yaml``."""

    model_config = ConfigDict(extra="forbid")

    providers: dict[str, ProviderConfig]
    instruments: list[InstrumentConfig]

    @model_validator(mode="after")
    def _check_references(self) -> Watchlist:
        unknown = {
            instrument.provider
            for instrument in self.instruments
            if instrument.provider not in self.providers
        }
        if unknown:
            msg = f"instruments reference unknown providers: {sorted(unknown)}"
            raise ValueError(msg)

        seen: set[tuple[str, str]] = set()
        for instrument in self.instruments:
            key = (instrument.provider, instrument.symbol)
            if key in seen:
                msg = f"duplicate instrument in watchlist: {key}"
                raise ValueError(msg)
            seen.add(key)
        return self

    def by_symbol(self, symbol: str, provider: str | None = None) -> InstrumentConfig:
        """Return the instrument matching ``symbol`` (and optionally ``provider``)."""

        matches = [
            instrument
            for instrument in self.instruments
            if instrument.symbol == symbol and (provider is None or instrument.provider == provider)
        ]
        if not matches:
            msg = f"instrument not found in watchlist: {symbol}"
            raise KeyError(msg)
        if len(matches) > 1:
            msg = f"symbol {symbol!r} is ambiguous; specify a provider"
            raise KeyError(msg)
        return matches[0]

    def active_instruments(self) -> list[InstrumentConfig]:
        return [instrument for instrument in self.instruments if instrument.active]


class PremiumConfig(BaseModel):
    """One premium payment declared in the configured policy YAML file."""

    model_config = ConfigDict(extra="forbid")

    paid_on: date
    amount: float = Field(gt=0)


class HoldingConfig(BaseModel):
    """One compartment of the policy declared in the configured policy YAML file."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1)
    value: float = Field(ge=0)
    quote: float | None = Field(default=None, ge=0)
    nav: float | None = Field(default=None, ge=0)


class PolicyConfig(BaseModel):
    """Validated content of the configured policy YAML file."""

    model_config = ConfigDict(extra="forbid")

    as_of: date
    currency: str = Field(default="EUR", min_length=3, max_length=3)
    tax_rate: float = Field(default=0.20, ge=0, le=1)
    premiums: list[PremiumConfig] = Field(min_length=1)
    holdings: list[HoldingConfig] = Field(min_length=1)

    @field_validator("currency")
    @classmethod
    def _upper_currency(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _check_dates(self) -> PolicyConfig:
        if self.as_of < max(premium.paid_on for premium in self.premiums):
            msg = "as_of cannot be before the last premium"
            raise ValueError(msg)
        return self


def normalize_database_url(url: str) -> str:
    """Use the psycopg 3 driver for plain PostgreSQL URLs.

    Neon and most providers hand out ``postgresql://`` URLs, which SQLAlchemy
    would route to psycopg2. The project ships psycopg 3, so normalise the
    scheme while leaving explicit driver URLs untouched.
    """

    for prefix in ("postgres://", "postgresql://"):
        if url.startswith(prefix):
            return f"postgresql+psycopg://{url.removeprefix(prefix)}"
    return url


def redact_database_url(url: str) -> str:
    """Return a URL safe to print in logs and API responses."""

    try:
        return make_url(url).render_as_string(hide_password=True)
    except ArgumentError:
        return "<invalid database URL>"


class Settings(BaseSettings):
    """Runtime settings, overridable through ``ASSETLENS_*`` variables."""

    model_config = SettingsConfigDict(
        env_prefix="ASSETLENS_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    data_dir: Path = Field(default_factory=lambda: project_root() / "data")
    database_url: str | None = None
    # Neon recommends a direct connection for migrations and a pooled one for
    # application traffic. Defaults to ``database_url`` when not set.
    migration_database_url: str | None = None
    instruments_file: Path = Field(
        default_factory=lambda: project_root() / "config" / "instruments.yaml"
    )
    policy_file: Path = Field(default_factory=lambda: project_root() / "config" / "policy.yaml")
    log_level: str = "INFO"
    http_timeout: float = Field(default=20.0, gt=0)
    http_user_agent: str = f"assetlens/{__version__} (+https://github.com/your-user/assetlens)"
    request_delay_seconds: float = Field(default=0.5, ge=0)
    max_retries: int = Field(default=4, ge=1, le=10)
    overlap_days: int = Field(default=5, ge=0, le=60)
    backfill_start: date = date(2000, 1, 1)

    @field_validator("data_dir", "instruments_file", "policy_file")
    @classmethod
    def _expand_path(cls, value: Path) -> Path:
        return value.expanduser().resolve()

    @property
    def resolved_database_url(self) -> str:
        """SQLAlchemy URL used by the application, normalised for its driver."""

        if self.database_url:
            return normalize_database_url(self.database_url)
        return f"sqlite:///{(self.data_dir / 'assetlens.db').as_posix()}"

    @property
    def resolved_migration_database_url(self) -> str:
        """URL used by Alembic, preferring the direct connection when provided."""

        if self.migration_database_url:
            return normalize_database_url(self.migration_database_url)
        return self.resolved_database_url

    @property
    def redacted_database_url(self) -> str:
        """Application URL with the password removed, safe for logs and APIs."""

        return redact_database_url(self.resolved_database_url)

    @property
    def redacted_migration_database_url(self) -> str:
        """Migration URL with the password removed, safe for logs."""

        return redact_database_url(self.resolved_migration_database_url)

    @property
    def database_path(self) -> Path | None:
        """Filesystem path of the SQLite database, or ``None`` for other backends."""

        prefix = "sqlite:///"
        url = self.resolved_database_url
        if url.startswith(prefix):
            return Path(url.removeprefix(prefix))
        return None


def load_watchlist(path: Path) -> Watchlist:
    """Load and validate the YAML watchlist at ``path``."""

    if not path.is_file():
        msg = f"watchlist not found: {path}"
        raise FileNotFoundError(msg)

    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)

    if not isinstance(raw, dict):
        msg = f"watchlist must be a mapping, got {type(raw).__name__}"
        raise ValueError(msg)

    return Watchlist.model_validate(raw)


def load_policy(path: Path) -> PolicyConfig:
    """Load and validate the policy snapshot at ``path``."""

    if not path.is_file():
        msg = f"policy file not found: {path}"
        raise FileNotFoundError(msg)

    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle)

    if not isinstance(raw, dict):
        msg = f"policy must be a mapping, got {type(raw).__name__}"
        raise ValueError(msg)

    return PolicyConfig.model_validate(raw)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return cached application settings."""

    return Settings()
