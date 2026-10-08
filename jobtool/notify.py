"""Extension-breakage alerts via a Discord webhook (log-only when no webhook is configured)."""
from __future__ import annotations

import logging
import threading
import time

import httpx

log = logging.getLogger(__name__)


class Notifier:
    def __init__(self, webhook_url: str = "", throttle_seconds: int = 3600):
        self.webhook_url = webhook_url
        self.throttle = throttle_seconds
        self._last_sent: dict[str, float] = {}
        self._lock = threading.Lock()

    def extension_failed(self, site_id: str, error: str) -> None:
        log.error("Extension %s failed: %s", site_id, error)
        if not self.webhook_url:
            return
        with self._lock:
            last = self._last_sent.get(site_id, 0.0)
            if time.time() - last < self.throttle:
                return  # one alert per extension per hour is enough
            self._last_sent[site_id] = time.time()
        content = f"⚠️ **jobtool**: extension `{site_id}` failed\n```{error[:1500]}```"
        try:
            httpx.post(self.webhook_url, json={"content": content}, timeout=10).raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("Could not send Discord alert: %s", exc)
