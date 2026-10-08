"""Base class and polite HTTP access for site extensions.

An extension lives in `extensions/<site_id>.py`, subclasses `Extension`, and implements
`search(filters) -> list[JobListing]`. It fetches everything through `self.http`, which
applies the extension's randomized delay and, for `access_method = "html"`, the robots.txt check.
"""
from __future__ import annotations

import logging
import random
import re
import time
from html import unescape
from typing import ClassVar, Literal

import httpx

from .models import JobListing, SearchFilters
from .robots import RobotsChecker

log = logging.getLogger(__name__)


class RobotsDisallowed(Exception):
    pass


class PoliteClient:
    def __init__(self, ext: "Extension", user_agent: str, robots: RobotsChecker):
        self.ext = ext
        self.robots = robots
        self.client = httpx.Client(
            headers={"User-Agent": user_agent, "Accept-Language": "de,en;q=0.8,sv;q=0.6"},
            follow_redirects=True,
            timeout=30,
        )
        self._last_request = 0.0
        self.skipped: list[str] = []

    def _wait(self) -> None:
        delay = random.uniform(self.ext.min_delay, self.ext.max_delay)
        remaining = self._last_request + delay - time.monotonic()
        if remaining > 0:
            time.sleep(remaining)
        self._last_request = time.monotonic()

    def get(self, url: str, **kwargs) -> httpx.Response | None:
        """GET with delay. Returns None (and logs) when robots.txt disallows the URL."""
        if self.ext.access_method == "html" and not self.robots.allowed(self.client, url):
            log.info("[%s] robots.txt disallows %s, skipping", self.ext.site_id, url)
            self.skipped.append(url)
            return None
        self._wait()
        resp = self.client.get(url, **kwargs)
        resp.raise_for_status()
        return resp

    def close(self) -> None:
        self.client.close()


class Extension:
    site_id: ClassVar[str]
    name: ClassVar[str]
    country: ClassVar[str]
    version: ClassVar[str] = "1.0"
    access_method: ClassVar[Literal["html", "api"]]
    public_demo_enabled: ClassVar[bool] = False
    enabled: ClassVar[bool] = True  # False = discovered but never run (e.g. blocked sites)
    disabled_reason: ClassVar[str] = ""
    base_url: ClassVar[str]
    min_delay: ClassVar[float] = 2.0
    max_delay: ClassVar[float] = 5.0
    language: ClassVar[str] = "en"  # language used for role translation

    def __init__(self, http: PoliteClient | None = None):
        self.http = http

    def search(self, filters: SearchFilters) -> list[JobListing]:
        raise NotImplementedError

    @classmethod
    def handles(cls, filters: SearchFilters) -> bool:
        return filters.country is None or filters.country == cls.country

    @classmethod
    def info(cls) -> dict:
        return {
            "site_id": cls.site_id,
            "name": cls.name,
            "country": cls.country,
            "version": cls.version,
            "access_method": cls.access_method,
            "public_demo_enabled": cls.public_demo_enabled,
            "enabled": cls.enabled,
            "disabled_reason": cls.disabled_reason,
            "base_url": cls.base_url,
        }


# --- Helpers shared by extensions -------------------------------------------------------------

ROLE_TRANSLATIONS: dict[str, dict[str, str]] = {
    "developer": {"de": "Entwickler", "sv": "utvecklare"},
    "software developer": {"de": "Softwareentwickler", "sv": "systemutvecklare"},
    "software engineer": {"de": "Software Engineer", "sv": "mjukvaruingenjör"},
    "engineer": {"de": "Ingenieur", "sv": "ingenjör"},
    "frontend developer": {"de": "Frontend Entwickler", "sv": "frontendutvecklare"},
    "backend developer": {"de": "Backend Entwickler", "sv": "backendutvecklare"},
    "fullstack developer": {"de": "Fullstack Entwickler", "sv": "fullstackutvecklare"},
    "data engineer": {"de": "Data Engineer", "sv": "data engineer"},
    "data scientist": {"de": "Data Scientist", "sv": "data scientist"},
    "devops engineer": {"de": "DevOps Engineer", "sv": "devops"},
    "tester": {"de": "Softwaretester", "sv": "testare"},
}


def translate_role(role: str, language: str) -> str:
    if not role or language == "en":
        return role
    return ROLE_TRANSLATIONS.get(role.strip().lower(), {}).get(language, role)


LEVEL_PATTERNS = {
    "junior": re.compile(r"\b(junior|jr\.?|entry|graduate|trainee|einsteiger|berufseinsteiger)\b", re.I),
    "senior": re.compile(r"\b(senior|sr\.?|erfahren(e|er)?)\b", re.I),
    "lead": re.compile(r"\b(lead|principal|head of|staff|architect|teamleit\w*|leitung)\b", re.I),
}


def infer_level(title: str) -> str:
    for level in ("lead", "senior", "junior"):
        if LEVEL_PATTERNS[level].search(title):
            return level
    return "mid"


REMOTE_RE = re.compile(r"\b(remote|home[\s-]?office|hybrid|distans|på distans|fernarbeit|mobiles arbeiten)\b", re.I)


def looks_remote(*texts: str) -> bool:
    return any(REMOTE_RE.search(t or "") for t in texts)


def apply_client_filters(jobs: list[JobListing], filters: SearchFilters) -> list[JobListing]:
    """Filters that most sites can't express in their URL: level and remote."""
    out = jobs
    if filters.level:
        out = [j for j in out if infer_level(j.title) == filters.level]
    if filters.remote:
        out = [j for j in out if j.remote]
    return out


_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_RE = re.compile(r"</?(p|div|br|li|ul|ol|h[1-6]|tr)\b[^>]*>", re.I)


def html_to_text(html: str) -> str:
    text = _BLOCK_RE.sub("\n", html or "")
    text = unescape(_TAG_RE.sub("", text))
    lines = [re.sub(r"[ \t ]+", " ", ln).strip() for ln in text.splitlines()]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines)).strip()


def slugify(text: str) -> str:
    text = text.lower()
    for src, dst in (("ä", "ae"), ("ö", "oe"), ("ü", "ue"), ("ß", "ss"), ("å", "a")):
        text = text.replace(src, dst)
    return re.sub(r"[^a-z0-9]+", "-", text).strip("-")
