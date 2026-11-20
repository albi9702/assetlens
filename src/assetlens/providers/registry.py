"""Provider registry.

Adding a new data source means writing a :class:`BaseProvider` subclass and
registering it here. No other layer needs to change.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from assetlens.config import ProviderConfig, Settings
from assetlens.errors import ConfigurationError
from assetlens.providers.base import BaseProvider
from assetlens.providers.generali import GeneraliProvider

if TYPE_CHECKING:
    import httpx

PROVIDER_REGISTRY: dict[str, type[BaseProvider]] = {
    GeneraliProvider.name: GeneraliProvider,
}


def build_provider(
    name: str,
    config: ProviderConfig,
    settings: Settings,
    *,
    client: httpx.Client | None = None,
) -> BaseProvider:
    """Instantiate the provider registered under ``name``."""

    provider_class = PROVIDER_REGISTRY.get(name)
    if provider_class is None:
        known = ", ".join(sorted(PROVIDER_REGISTRY))
        msg = f"unknown provider {name!r}; available providers: {known}"
        raise ConfigurationError(msg)
    return provider_class(config, settings, client=client)
