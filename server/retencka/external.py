"""Outbound HTTP: Open-Meteo forecast and Hunter Hydrawise (legacy REST API)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Optional

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
        self._key: Optional[tuple] = None
        self._fetched = 0.0
        self._lock = asyncio.Lock()

    async def forecast(self, lat: float, lon: float) -> Optional[dict]:
        """Return the cached forecast, refreshing it first when older than 1 h."""
        key = (round(lat, 3), round(lon, 3))
        async with self._lock:
            if time.time() - self._fetched >= 3600 or self._key != key:
                try:
                    params = {"latitude": lat, "longitude": lon, "timezone": "auto", "forecast_days": 7,
                              "daily": "precipitation_sum,temperature_2m_max,temperature_2m_min"}
                    status, body = await self.http.get(self.URL, params)
                    if status != 200:
                        raise RuntimeError(f"HTTP {status}")
                    d = json.loads(body).get("daily", {})
                    self._data = {
                        "time": d.get("time", [])[:7],
                        "precipitation_sum": [x or 0.0 for x in d.get("precipitation_sum", [])][:7],
                        "temperature_2m_max": d.get("temperature_2m_max", [])[:7],
                        "temperature_2m_min": d.get("temperature_2m_min", [])[:7],
                    }
                    self._key = key
                    self._fetched = time.time()
                except Exception as e:
                    log.warning("Open-Meteo failed: %s", e)
                    self._fetched = time.time() - 3000      # retry in ~10 min
            return self._data

    def cached(self, lat: float, lon: float) -> Optional[dict]:
        """Non-blocking: cached data now, refresh in the background when stale."""
        key = (round(lat, 3), round(lon, 3))
        if (time.time() - self._fetched >= 3600 or self._key != key) and not self._lock.locked():
            asyncio.ensure_future(self.forecast(lat, lon))
        return self._data if self._key == key else None

    async def rain_next_2_days(self, lat: float, lon: float, wait: bool = True) -> Optional[float]:
        d = await self.forecast(lat, lon) if wait else self.cached(lat, lon)
        if not d or not d["precipitation_sum"]:
            return None
        return round(sum(d["precipitation_sum"][:2]), 1)


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
