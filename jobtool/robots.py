"""robots.txt parsing with Google/RFC 9309 semantics.

The stdlib parser uses first-match rule order and ignores wildcards, so a file that starts with
`Allow: /` (like devjobs.at's) would allow everything. Here the most specific (longest) matching
rule wins, `Allow` wins ties, and `*` / `$` are supported.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import httpx

log = logging.getLogger(__name__)


@dataclass
class _Group:
    agents: list[str] = field(default_factory=list)
    rules: list[tuple[bool, str]] = field(default_factory=list)  # (allow, pattern)


class RobotsRules:
    def __init__(self, text: str):
        self.groups: list[_Group] = []
        current: _Group | None = None
        last_was_agent = False
        for raw in text.splitlines():
            line = raw.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, _, value = line.partition(":")
            key, value = key.strip().lower(), value.strip()
            if key == "user-agent":
                if current is None or not last_was_agent:
                    current = _Group()
                    self.groups.append(current)
                current.agents.append(value.lower())
                last_was_agent = True
            elif key in ("allow", "disallow") and current is not None:
                last_was_agent = False
                if value:  # empty Disallow means "allow all"
                    current.rules.append((key == "allow", value))
            else:
                last_was_agent = False

    def _rules_for(self, user_agent: str) -> list[tuple[bool, str]]:
        token = user_agent.split("/", 1)[0].strip().lower()
        specific = [g for g in self.groups if any(a != "*" and a in token for a in g.agents)]
        if specific:
            return [r for g in specific for r in g.rules]
        return [r for g in self.groups if "*" in g.agents for r in g.rules]

    @staticmethod
    def _matches(pattern: str, path: str) -> bool:
        anchored = pattern.endswith("$")
        body = pattern[:-1] if anchored else pattern
        regex = "".join(".*" if ch == "*" else re.escape(ch) for ch in body)
        return re.match(regex + ("$" if anchored else ""), path) is not None

    def can_fetch(self, user_agent: str, url: str) -> bool:
        parts = urlsplit(url)
        path = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        best_len, allowed = -1, True
        for allow, pattern in self._rules_for(user_agent):
            if self._matches(pattern, path):
                length = len(pattern)
                if length > best_len or (length == best_len and allow):
                    best_len, allowed = length, allow
        return allowed


class RobotsChecker:
    """Fetches and caches robots.txt per host."""

    def __init__(self, user_agent: str, ttl_seconds: int = 24 * 3600):
        self.user_agent = user_agent
        self.ttl = ttl_seconds
        self._cache: dict[str, tuple[float, RobotsRules]] = {}
        self._lock = threading.Lock()

    def _rules(self, client: httpx.Client, origin: str) -> RobotsRules:
        with self._lock:
            hit = self._cache.get(origin)
            if hit and time.time() - hit[0] < self.ttl:
                return hit[1]
        try:
            resp = client.get(f"{origin}/robots.txt", headers={"User-Agent": self.user_agent})
            if resp.status_code >= 500:
                # Server error: treat as fully disallowed, per RFC 9309.
                rules = RobotsRules("User-agent: *\nDisallow: /")
            elif resp.status_code >= 400:
                rules = RobotsRules("")
            else:
                rules = RobotsRules(resp.text)
        except httpx.HTTPError as exc:
            log.warning("robots.txt unreachable for %s (%s); treating as disallowed", origin, exc)
            rules = RobotsRules("User-agent: *\nDisallow: /")
        with self._lock:
            self._cache[origin] = (time.time(), rules)
        return rules

    def allowed(self, client: httpx.Client, url: str) -> bool:
        parts = urlsplit(url)
        return self._rules(client, f"{parts.scheme}://{parts.netloc}").can_fetch(self.user_agent, url)
