"""Open-Meteo archive fetch shared by the weather tool (`tools.get_weather`) and the analyzer's growing degree days.

Free, no key. The archive lags about two days, so the newest days may come back null."""
from __future__ import annotations

from datetime import date

import requests

ARCHIVE_URL = "https://archive-api.open-meteo.com/v1/archive"


def archive_daily(lat: float, lon: float, start: date, end: date, daily: str, *, timeout: float = 20,
                  http_get=requests.get, **units) -> dict:
    """The `daily` block (name -> list of values, plus `time`) for lat/lon between start and end inclusive.
    `units` are extra query params such as temperature_unit="fahrenheit". Raises on network or format errors."""
    params = {"latitude": lat, "longitude": lon, "start_date": start.isoformat(), "end_date": end.isoformat(),
              "daily": daily, "timezone": "America/Chicago", **units}
    body = http_get(ARCHIVE_URL, params=params, timeout=timeout).json()
    if "daily" not in body:  # rate limit or bad request: say why instead of a bare KeyError
        raise RuntimeError(f"Open-Meteo: {body.get('reason', 'no daily data in the response')}")
    return body["daily"]


def daily_temps_f(lat: float, lon: float, start: date, end: date, *,
                  http_get=requests.get) -> list[tuple[date, float | None, float | None]]:
    """[(day, tmin_F, tmax_F)] for the growing-degree-day math; None where the archive has no value yet."""
    d = archive_daily(lat, lon, start, end, "temperature_2m_min,temperature_2m_max", http_get=http_get,
                      temperature_unit="fahrenheit")
    return [(date.fromisoformat(t), lo, hi) for t, lo, hi in zip(d["time"], d["temperature_2m_min"], d["temperature_2m_max"])]
