"""Generali asset-management quotes.

The public page renders a chart from a small JSON API:

``POST /api/quotazioniApi/GetQuotazioniFondo`` with body
``{"codiceFondo": "...", "da": "...", "a": "..."}``.

Dates in the response use the legacy .NET format ``/Date(<epoch-ms>)/``.
"""

from __future__ import annotations

import math
import re
from datetime import UTC, date, datetime, time, timedelta, timezone
from typing import Any, ClassVar
from zoneinfo import ZoneInfo

import polars as pl

from assetlens.errors import ProviderResponseError
from assetlens.providers.base import BaseProvider, normalize_observation_frame

DOTNET_DATE_RE = re.compile(r"/Date\((?P<milliseconds>-?\d+)(?P<offset>[+-]\d{4})?\)/")
ROME = ZoneInfo("Europe/Rome")


def parse_dotnet_date(value: str) -> date:
    """Convert a .NET ``/Date(...)/`` string into a calendar date.

    ``/Date(1573776000000)/`` is an absolute UTC instant; when an offset is
    present (``/Date(1573776000000+0100)/``) it represents the local time of the
    source, so the offset is applied before extracting the date.
    """

    match = DOTNET_DATE_RE.fullmatch(value.strip())
    if match is None:
        msg = f"unsupported .NET date format: {value!r}"
        raise ProviderResponseError(msg)

    milliseconds = int(match.group("milliseconds"))
    try:
        moment = datetime.fromtimestamp(milliseconds / 1000, tz=UTC)
    except (OverflowError, OSError, ValueError) as exc:
        msg = f"invalid .NET timestamp: {value!r}"
        raise ProviderResponseError(msg) from exc

    raw_offset = match.group("offset")
    if raw_offset:
        sign = 1 if raw_offset[0] == "+" else -1
        minutes = sign * (int(raw_offset[1:3]) * 60 + int(raw_offset[3:5]))
        moment = moment.astimezone(timezone(timedelta(minutes=minutes)))

    return moment.date()


def _format_boundary(day: date, *, end_of_day: bool) -> str:
    moment = datetime.combine(
        day,
        time(23, 59, 59) if end_of_day else time(0, 0, 0),
        tzinfo=ROME,
    )
    return moment.isoformat(timespec="seconds")


class GeneraliProvider(BaseProvider):
    """Adapter for the Generali quotes API."""

    name: ClassVar[str] = "generali"
    endpoint: ClassVar[str] = "/api/quotazioniApi/GetQuotazioniFondo"
    page_url: ClassVar[str] = "https://asset.generali.it/quotazioni/genera-sviluppo-sostenibile"
    default_headers: ClassVar[dict[str, str]] = {"Referer": page_url}

    def fetch_history(self, symbol: str, start: date, end: date) -> pl.DataFrame:
        if start > end:
            msg = f"start date {start} is after end date {end}"
            raise ValueError(msg)

        payload = {
            "codiceFondo": symbol,
            "da": _format_boundary(start, end_of_day=False),
            "a": _format_boundary(end, end_of_day=True),
        }
        response = self._request(
            "POST",
            self.endpoint,
            json=payload,
            headers={"Content-Type": "application/json; charset=utf-8"},
        )
        data = self._decode_json(response)
        return self._parse_payload(data, symbol=symbol)

    def _parse_payload(self, data: Any, *, symbol: str) -> pl.DataFrame:
        if not isinstance(data, list):
            msg = f"{self.name}: expected a JSON array for {symbol}, got {type(data).__name__}"
            raise ProviderResponseError(msg)

        records: list[tuple[date, float]] = []
        for index, item in enumerate(data):
            if not isinstance(item, dict) or "Data" not in item or "Valore" not in item:
                msg = f"{self.name}: malformed row at index {index} for {symbol}"
                raise ProviderResponseError(msg)
            try:
                value = float(item["Valore"])
            except (TypeError, ValueError) as exc:
                msg = f"{self.name}: invalid value at index {index} for {symbol}"
                raise ProviderResponseError(msg) from exc
            if not math.isfinite(value):
                msg = f"{self.name}: invalid value at index {index} for {symbol}: {value!r}"
                raise ProviderResponseError(msg)
            records.append((parse_dotnet_date(str(item["Data"])), value))

        return normalize_observation_frame(records)
