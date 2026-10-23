"""Outbound HTTP: Open-Meteo forecast and Hunter Hydrawise (legacy REST API)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Optional

RAIN_PAST_DAYS = 92              # Open-Meteo maximum for the forecast endpoint

log = logging.getLogger("retencka.external")


class Http:
    """Tiny HTTPS client: urllib in a worker thread (no extra dependencies)."""

    def __init__(self, timeout: float = 10.0) -> None:
        self.timeout = timeout
        self._sem = asyncio.Semaphore(2)

    def _get(self, url: str, params: dict) -> tuple[int, str]:
        full = url + "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(full, headers={"User-Agent": "retencka/3", "Accept": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as r:
                return r.status, r.read(256 * 1024).decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            return e.code, e.read(4096).decode("utf-8", "replace")

    async def get(self, url: str, params: dict) -> tuple[int, str]:
        async with self._sem:
            return await asyncio.to_thread(self._get, url, params)

    async def close(self) -> None:
        pass


class Weather:
    """7-day forecast cached for one hour; failures keep the last good value."""

    URL = "https://api.open-meteo.com/v1/forecast"

    def __init__(self, http: Http):
        self.http = http
        self._data: Optional[dict] = None
        self._rain: list[tuple[int, float]] = []
        self._key: Optional[tuple] = None
        self._fetched = 0.0
        self._attempt_key: Optional[tuple] = None    # key / time of the last attempt (success or failure)
        self._attempt_at = 0.0
        self._lock = asyncio.Lock()

    def _due(self, key: tuple) -> bool:
        """Refresh needed? Good data older than 1 h or for another place; a failed attempt
        backs off for ~10 min (unless the place changed)."""
        now = time.time()
        if self._attempt_key != key:
            return True
        if self._key == key:
            return now - self._fetched >= 3600
        return now - self._attempt_at >= 600

    async def forecast(self, lat: float, lon: float) -> Optional[dict]:
        """Return the cached forecast, refreshing it first when older than 1 h."""
        key = (round(lat, 3), round(lon, 3))
        async with self._lock:
            if self._due(key):
                self._attempt_key, self._attempt_at = key, time.time()
                try:
                    # days follow the location's calendar (timezone=auto); all times come
                    # as epoch seconds, so nothing depends on the time zone of the Pi
                    params = {"latitude": lat, "longitude": lon, "timezone": "auto", "timeformat": "unixtime",
                              "forecast_days": 7, "past_days": RAIN_PAST_DAYS, "hourly": "precipitation",
                              "daily": "precipitation_sum,temperature_2m_max,temperature_2m_min"}
                    status, body = await self.http.get(self.URL, params)
                    if status != 200:
                        raise RuntimeError(f"HTTP {status}")
                    js = json.loads(body)
                    offset = int(js.get("utc_offset_seconds") or 0)
                    d = js.get("daily", {})
                    today = _local_date(time.time(), offset)
                    # daily time = local midnight; noon is safe across a DST change
                    days = [_local_date(t + 43200, offset) for t in d.get("time", [])]
                    i0 = days.index(today) if today in days else max(0, len(days) - 7)
                    self._data = {
                        "time": days[i0:i0 + 7],
                        "precipitation_sum": [x or 0.0 for x in d.get("precipitation_sum", [])][i0:i0 + 7],
                        "temperature_2m_max": d.get("temperature_2m_max", [])[i0:i0 + 7],
                        "temperature_2m_min": d.get("temperature_2m_min", [])[i0:i0 + 7],
                    }
                    h = js.get("hourly", {})
                    # hourly rain (past ~3 months + forecast) as (epoch s, mm); the value at
                    # time T is the rain of the hour ENDING at T
                    self._rain = [(int(t), float(mm))
                                  for t, mm in zip(h.get("time", []), h.get("precipitation", []))
                                  if mm is not None]
                    self._key = key
                    self._fetched = time.time()
                except Exception as e:
                    log.warning("Open-Meteo failed: %s", e)
                    # data stays; _due() retries after the back-off
            return self._data

    def cached(self, lat: float, lon: float) -> Optional[dict]:
        """Non-blocking: cached data now, refresh in the background when stale."""
        key = (round(lat, 3), round(lon, 3))
        if self._due(key) and not self._lock.locked():
            asyncio.ensure_future(self.forecast(lat, lon))
        return self._data if self._key == key else None

    def rain_ahead(self, hours: float) -> Optional[tuple[float, Optional[int]]]:
        """Forecast rain in the next `hours` (mm) and the epoch when the last rainy
        hour in that window ends. None without forecast data."""
        if not self._rain:
            return None
        now = time.time()
        total, last = 0.0, None
        for ts, mm in self._rain:              # ts = end of the hour
            if now < ts <= now + hours * 3600 and mm > 0:
                total += mm
                last = ts
        return round(total, 1), last

    def rain_next_days(self, days: int) -> Optional[float]:
        d = self._data
        if not d or not d["precipitation_sum"]:
            return None
        return round(sum(d["precipitation_sum"][:days]), 1)

    async def rain_next_2_days(self, lat: float, lon: float, wait: bool = True) -> Optional[float]:
        d = await self.forecast(lat, lon) if wait else self.cached(lat, lon)
        if not d or not d["precipitation_sum"]:
            return None
        return round(sum(d["precipitation_sum"][:2]), 1)


def _local_date(epoch: float, utc_offset_s: int) -> str:
    return datetime.fromtimestamp(epoch + utc_offset_s, timezone.utc).date().isoformat()


class Hydrawise:
    URL = "https://api.hydrawise.com/api/v1/setzone.php"

    def __init__(self, http: Http):
        self.http = http

    async def call(self, api_key: str, relay_id: str, action: str, seconds: int = 0) -> tuple[bool, str]:
        if not api_key or not relay_id:
            return False, "Hydrawise není nastaven (API klíč / Relay ID)."
        params: dict[str, Any] = {"api_key": api_key, "action": action, "relay_id": relay_id}
        if action == "run":
            params["custom"] = int(seconds)
            params["period_id"] = 999
        for attempt in range(3):
            try:
                status, text = await self.http.get(self.URL, params)
                text = text[:300]
                if status == 200 and '"error"' not in text:
                    log.info("Hydrawise %s OK", action)
                    return True, "OK"
                log.warning("Hydrawise %s HTTP %s: %s", action, status, text.replace(api_key, "***"))
                msg = f"Hydrawise odpověděl chybou (HTTP {status})."
            except Exception as e:
                log.warning("Hydrawise %s failed: %s", action, e)
                msg = "Hydrawise není dostupný."
            await asyncio.sleep(2 * (attempt + 1))
        return False, msg
