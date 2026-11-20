"""Tests for the Generali adapter."""

from __future__ import annotations

from datetime import date

import httpx
import polars as pl
import pytest
import respx

from assetlens.config import ProviderConfig, Settings
from assetlens.errors import ProviderHTTPError, ProviderResponseError
from assetlens.providers.generali import GeneraliProvider, parse_dotnet_date


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("/Date(1573776000000)/", date(2019, 11, 15)),
        ("/Date(1573776000000+0100)/", date(2019, 11, 15)),
        ("/Date(1573776000000-0500)/", date(2019, 11, 14)),
    ],
)
def test_parse_dotnet_date(raw: str, expected: date) -> None:
    assert parse_dotnet_date(raw) == expected


def test_parse_dotnet_date_rejects_invalid_payload() -> None:
    with pytest.raises(ProviderResponseError):
        parse_dotnet_date("2024-01-01")


def _provider(settings: Settings, **overrides: object) -> GeneraliProvider:
    config = ProviderConfig(
        base_url="https://asset.generali.it",
        request_delay_seconds=0,
        **overrides,
    )
    return GeneraliProvider(config, settings)


@respx.mock
def test_fetch_history_returns_normalized_frame(
    settings: Settings, generali_payload: list[dict[str, object]]
) -> None:
    route = respx.post("https://asset.generali.it/api/quotazioniApi/GetQuotazioniFondo").mock(
        return_value=httpx.Response(200, json=generali_payload)
    )
    provider = _provider(settings)

    frame = provider.fetch_history("GI_GEN_TUTELA_CLIMA", date(2019, 11, 1), date(2019, 11, 20))

    assert route.called
    assert frame.schema["price_date"] == pl.Date
    assert frame.schema["value"] == pl.Float64
    assert frame.height == 3
    assert frame["price_date"].to_list() == [
        date(2019, 11, 15),
        date(2019, 11, 18),
        date(2019, 11, 19),
    ]

    request = route.calls[0].request
    body = request.read().decode()
    assert '"codiceFondo":"GI_GEN_TUTELA_CLIMA"' in body
    assert "2019-11-01T00:00:00+01:00" in body
    assert "2019-11-20T23:59:59+01:00" in body
    assert request.headers["Referer"] == GeneraliProvider.page_url


@respx.mock
def test_fetch_history_rejects_malformed_rows(settings: Settings) -> None:
    respx.post("https://asset.generali.it/api/quotazioniApi/GetQuotazioniFondo").mock(
        return_value=httpx.Response(200, json=[{"Data": "/Date(1573776000000)/"}])
    )
    provider = _provider(settings)

    with pytest.raises(ProviderResponseError):
        provider.fetch_history("GI_GEN_TUTELA_CLIMA", date(2019, 11, 1), date(2019, 11, 20))


@respx.mock
def test_client_errors_are_not_retried(settings: Settings) -> None:
    route = respx.post("https://asset.generali.it/api/quotazioniApi/GetQuotazioniFondo").mock(
        return_value=httpx.Response(404)
    )
    provider = _provider(settings)

    with pytest.raises(ProviderHTTPError) as error:
        provider.fetch_history("UNKNOWN", date(2024, 1, 1), date(2024, 1, 2))

    assert error.value.status_code == 404
    assert route.call_count == 1


@respx.mock
def test_fetch_gesav_returns_a_rate_series(
    settings: Settings, gesav_payload: list[dict[str, object]]
) -> None:
    respx.post("https://asset.generali.it/api/quotazioniApi/GetQuotazioniFondo").mock(
        return_value=httpx.Response(200, json=gesav_payload)
    )
    provider = _provider(settings)

    frame = provider.fetch_history("GESAV", date(2024, 1, 1), date(2026, 12, 31))

    assert frame.height == 6
    assert frame["price_date"].to_list() == [
        date(2024, 1, 1),
        date(2024, 6, 1),
        date(2024, 12, 1),
        date(2025, 1, 1),
        date(2025, 12, 1),
        date(2026, 1, 1),
    ]
    assert frame["value"].to_list() == [2.94, 2.97, 2.97, 2.99, 3.10, 3.11]


@respx.mock
def test_provider_does_not_impose_price_semantics(settings: Settings) -> None:
    payload = [
        {"Data": "/Date(1704067200000)/", "Valore": 0.0},
        {"Data": "/Date(1706745600000)/", "Valore": -2.5},
    ]
    respx.post("https://asset.generali.it/api/quotazioniApi/GetQuotazioniFondo").mock(
        return_value=httpx.Response(200, json=payload)
    )
    provider = _provider(settings)

    frame = provider.fetch_history("GESAV", date(2024, 1, 1), date(2024, 2, 29))

    assert frame["value"].to_list() == [0.0, -2.5]


@respx.mock
def test_fetch_history_rejects_non_finite_values(settings: Settings) -> None:
    respx.post("https://asset.generali.it/api/quotazioniApi/GetQuotazioniFondo").mock(
        return_value=httpx.Response(200, json=[{"Data": "/Date(1704067200000)/", "Valore": "NaN"}])
    )
    provider = _provider(settings)

    with pytest.raises(ProviderResponseError):
        provider.fetch_history("GESAV", date(2024, 1, 1), date(2024, 1, 2))
