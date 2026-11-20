"""Provider abstraction.

A provider turns a provider-specific symbol (a Generali fund code, a Yahoo
ticker, an ISIN, ...) into a normalized Polars frame with two columns:

* ``price_date`` - ``pl.Date`` in the Europe/Rome calendar
* ``value`` - ``pl.Float64``, full precision as published by the source

Every provider owns an ``httpx.Client`` and inherits throttling plus retry
logic, so adapters only implement payload encoding and response parsing.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, timedelta
from types import TracebackType
from typing import Any, ClassVar, Self

import httpx
import polars as pl
from tenacity import Retrying, retry_if_exception, stop_after_attempt, wait_exponential_jitter

from assetlens.config import ProviderConfig, Settings
from assetlens.errors import ProviderHTTPError, ProviderResponseError

HTTP_BAD_REQUEST = 400
HTTP_TOO_MANY_REQUESTS = 429
HTTP_SERVER_ERROR = 500

OBSERVATION_SCHEMA: dict[str, Any] = {
    "price_date": pl.Date,
    "value": pl.Float64,
}


@dataclass(frozen=True, slots=True)
class ObservationPoint:
    """A single observation returned by :meth:`BaseProvider.fetch_latest`."""

    price_date: date
    value: float


def normalize_observation_frame(records: list[tuple[date, float]]) -> pl.DataFrame:
    """Build a validated, sorted, de-duplicated observation frame."""

    if not records:
        return pl.DataFrame(schema=OBSERVATION_SCHEMA)

    frame = pl.DataFrame(
        {
            "price_date": [record[0] for record in records],
            "value": [record[1] for record in records],
        },
        schema=OBSERVATION_SCHEMA,
    )
    return frame.unique(subset=["price_date"], keep="last").sort("price_date")


def _is_retryable(error: BaseException) -> bool:
    if isinstance(error, ProviderHTTPError):
        return error.status_code == HTTP_TOO_MANY_REQUESTS or error.status_code >= HTTP_SERVER_ERROR
    return isinstance(error, httpx.TransportError)


class BaseProvider(ABC):
    """Common HTTP behaviour for every provider adapter."""

    name: ClassVar[str]
    default_headers: ClassVar[dict[str, str]] = {}

    def __init__(
        self,
        config: ProviderConfig,
        settings: Settings,
        *,
        client: httpx.Client | None = None,
    ) -> None:
        self.config = config
        self.settings = settings
        self._owns_client = client is None
        self._client = client or httpx.Client(
            base_url=config.base_url,
            timeout=config.timeout_seconds or settings.http_timeout,
            follow_redirects=True,
            headers={
                "User-Agent": settings.http_user_agent,
                "Accept": "application/json",
                **self.default_headers,
            },
        )
        self._delay = (
            config.request_delay_seconds
            if config.request_delay_seconds is not None
            else settings.request_delay_seconds
        )
        self._max_retries = config.max_retries or settings.max_retries
        self._last_request_at = 0.0

    def close(self) -> None:
        if self._owns_client:
            self._client.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        self.close()

    def _throttle(self) -> None:
        if self._delay <= 0:
            return
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self._delay:
            time.sleep(self._delay - elapsed)

    def _request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        """Perform an HTTP request with throttling and exponential backoff."""

        retryer = Retrying(
            stop=stop_after_attempt(self._max_retries),
            wait=wait_exponential_jitter(multiplier=0.5, max=8.0),
            retry=retry_if_exception(_is_retryable),
            reraise=True,
        )

        def _send() -> httpx.Response:
            self._throttle()
            response = self._client.request(method, url, **kwargs)
            self._last_request_at = time.monotonic()
            if response.status_code >= HTTP_BAD_REQUEST:
                message = f"{self.name}: HTTP {response.status_code} for {method} {url}"
                raise ProviderHTTPError(message, status_code=response.status_code)
            return response

        return retryer(_send)

    def _decode_json(self, response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            msg = f"{self.name}: response is not valid JSON"
            raise ProviderResponseError(msg) from exc

    @abstractmethod
    def fetch_history(self, symbol: str, start: date, end: date) -> pl.DataFrame:
        """Return normalized prices for ``symbol`` between ``start`` and ``end``."""

    def fetch_latest(self, symbol: str, *, lookback_days: int = 14) -> ObservationPoint | None:
        """Return the most recent observation, if any."""

        today = date.today()
        frame = self.fetch_history(symbol, today - timedelta(days=lookback_days), today)
        if frame.is_empty():
            return None
        last = frame.row(-1, named=True)
        return ObservationPoint(price_date=last["price_date"], value=last["value"])
