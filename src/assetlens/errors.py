"""Domain exceptions raised by assetlens."""

from __future__ import annotations


class AssetLensError(Exception):
    """Base class for every application error."""


class ConfigurationError(AssetLensError):
    """The configuration or watchlist is invalid."""


class InstrumentNotFoundError(AssetLensError):
    """The requested instrument is not stored in the database."""


class PolicySnapshotNotFoundError(AssetLensError):
    """The requested policy snapshot is not stored in the database."""


class ProviderError(AssetLensError):
    """A data provider failed."""


class ProviderHTTPError(ProviderError):
    """A provider returned an unexpected HTTP status."""

    def __init__(self, message: str, *, status_code: int) -> None:
        super().__init__(message)
        self.status_code = status_code


class ProviderResponseError(ProviderError):
    """A provider returned a payload that cannot be parsed."""
