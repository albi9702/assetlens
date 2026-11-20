# Providers

## Contract

A provider converts a provider-specific symbol into a normalized Polars frame.

```python
class BaseProvider(ABC):
    name: ClassVar[str]

    @abstractmethod
    def fetch_history(self, symbol: str, start: date, end: date) -> pl.DataFrame: ...
```

The returned frame must have exactly these columns:

| Column | Polars type | Meaning |
| --- | --- | --- |
| `price_date` | `pl.Date` | Calendar date in Europe/Rome. |
| `value` | `pl.Float64` | Price or return rate, full source precision. |

Rows must be sorted by date and unique per date. The helper
`normalize_observation_frame(records)` enforces sorting and de-duplication.
Providers do not decide whether a value is a price or a rate: they only reject
non-finite numbers. The repository enforces the instrument `value_type`.

`BaseProvider` also provides:

- an `httpx.Client` with base URL, timeout, User-Agent and provider headers;
- `_throttle()` with a configurable delay between requests;
- `_request()` with exponential backoff and jitter on timeouts, transport errors,
  429 and 5xx; 4xx are returned immediately;
- `fetch_latest()` implemented on top of `fetch_history()`.

Errors are typed: `ProviderHTTPError` carries the status code,
`ProviderResponseError` signals an unparsable payload.

## Adding a provider

1. Create `src/assetlens/providers/<name>.py`:

```python
from __future__ import annotations

from datetime import date

import polars as pl

from assetlens.providers.base import BaseProvider, normalize_observation_frame


class ExampleProvider(BaseProvider):
    name = "example"

    def fetch_history(self, symbol: str, start: date, end: date) -> pl.DataFrame:
        response = self._request(
            "GET",
            f"/api/prices/{symbol}",
            params={"from": start.isoformat(), "to": end.isoformat()},
        )
        payload = self._decode_json(response)
        records = [
            (date.fromisoformat(row["date"]), float(row["value"]))
            for row in payload
        ]
        return normalize_observation_frame(records)
```

2. Register it in `src/assetlens/providers/registry.py`:

```python
PROVIDER_REGISTRY = {
    GeneraliProvider.name: GeneraliProvider,
    ExampleProvider.name: ExampleProvider,
}
```

3. Add provider and instrument entries to `config/instruments.yaml`.

4. Add tests with `respx`, using a recorded payload.

## Generali adapter

### Endpoint

```
POST https://asset.generali.it/api/quotazioniApi/GetQuotazioniFondo
Content-Type: application/json; charset=utf-8
Referer: https://asset.generali.it/quotazioni/genera-sviluppo-sostenibile
```

Request body:

```json
{
  "codiceFondo": "GI_GEN_TUTELA_CLIMA",
  "da": "2019-11-15T00:00:00+01:00",
  "a": "2026-11-20T23:59:59+01:00"
}
```

The endpoint honours the date window and returns business-day observations.

### Response

```json
[
  {"Data": "/Date(1573776000000)/", "Valore": 10.0},
  {"Data": "/Date(1574035200000)/", "Valore": 10.044779}
]
```

`Data` uses the legacy .NET format: milliseconds since the Unix epoch in UTC.
The adapter also supports the variant with a local offset
(`/Date(1573776000000+0100)/`), applying it before extracting the date.

`Valore` is a decimal with up to six digits. The adapter rejects non-finite
values and malformed rows instead of silently dropping data.

### Separate accounts: GESAV

GESAV is a *gestione separata* (separate account), not a fund. The same
`GetQuotazioniFondo` endpoint returns its observations with the fund code
`GESAV`, but the values are **annual return rates**, not a NAV:

```json
[
  {"Data": "/Date(347155200000)/", "Valore": 19.65},
  {"Data": "/Date(1764547200000)/", "Valore": 3.13}
]
```

- The first observation is 1981-01-01 with 19.65 (%).
- From 1999 the series is monthly; before that it is yearly.
- The latest observation is the current year's running estimate (3.13% for
  2026-11-01 in the recorded dataset).
- The official year-end figure can differ slightly from the last monthly
  estimate: the 2025 rendiconto reports 3.13% while the last 2025 observation
  in the API is 3.10%.

Because of this, the watchlist entry uses `frequency: monthly`,
`value_type: return_pct` and `backfill_start: 1981-01-01`. The provider is
unchanged: it returns the raw values and the repository stores them as a return
series. A full sync stores 349 observations.

### Tracked compartments

| Symbol | Name |
| --- | --- |
| `GI_GEN_TUTELA_CLIMA` | Generali Tutela del Clima |
| `GI_GEN_CONSUMO_RESPONSABILE` | Generali Consumo Responsabile |
| `GI_GEN_PARI_OPPORTUNITA` | Generali Pari Opportunità |
| `GI_GEN_SALUTE_BENESSERE` | Generali Salute e Benessere |
| `GI_GEN_CRESCITA_SOSTENIBILE` | Generali Crescita Sostenibile |

All five are compartments of the *Genera Sviluppo Sostenibile* page. Their
history starts on 2019-11-15 and currently contains 1,761 business-day rows
each. GESAV is tracked separately as a monthly return series.

### Error handling

- 429 and 5xx are retried up to `max_retries` (default 4) with exponential
  backoff and jitter.
- 4xx are not retried: an unknown `codiceFondo` should fail fast.
- A non-array payload, a row without `Data`/`Valore`, or a non-finite value
  raises `ProviderResponseError`.
- Negative or zero values are accepted by the provider; the repository rejects
  them for `price` instruments and accepts them for `return_pct` instruments.

### Rate limiting and terms

The default delay is 0.5 seconds between requests. A full backfill of the five
funds performs five requests. Keep the delay for scheduled runs and check the
provider's terms before scraping at a higher rate.

## Testing providers

Use `respx` to intercept HTTP calls and a recorded payload:

```python
@respx.mock
def test_fetch(settings):
    respx.post("https://asset.generali.it/api/quotazioniApi/GetQuotazioniFondo").mock(
        return_value=httpx.Response(200, json=payload)
    )
    provider = GeneraliProvider(config, settings)
    frame = provider.fetch_history("GI_GEN_TUTELA_CLIMA", date(2019, 11, 1), date(2019, 11, 20))
    assert frame.height == 3
```

Never call the real provider from the test suite.
