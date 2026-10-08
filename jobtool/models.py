"""Core data types shared by the extensions, the scraper service and the dashboard."""
from __future__ import annotations

import hashlib
import json
from datetime import date
from typing import Literal

from pydantic import BaseModel, Field, field_validator

Level = Literal["junior", "mid", "senior", "lead"]
Country = Literal["AT", "SE", "DE"]


class SearchFilters(BaseModel):
    """Unified filters. Each extension maps these to its own URL scheme or API params."""

    role: str = ""  # English role term, e.g. "developer"; extensions translate it
    location: str = ""  # city, e.g. "Wien", "Stockholm"
    country: Country | None = None  # None = all countries
    keywords: list[str] = Field(default_factory=list)  # e.g. ["python", "django"]
    level: Level | None = None
    remote: bool = False

    @field_validator("keywords", mode="before")
    @classmethod
    def _split_keywords(cls, v):
        if isinstance(v, str):
            v = v.replace(",", " ").split()
        return [k.strip() for k in v if k and k.strip()]

    @field_validator("role", "location", mode="before")
    @classmethod
    def _strip(cls, v):
        return (v or "").strip()

    def cache_key(self) -> str:
        """Stable hash used as the result-cache key (together with the site id)."""
        data = self.model_dump()
        data["role"] = data["role"].lower()
        data["location"] = data["location"].lower()
        data["keywords"] = sorted(k.lower() for k in data["keywords"])
        raw = json.dumps(data, sort_keys=True)
        return hashlib.sha256(raw.encode()).hexdigest()[:16]

    def describe(self) -> str:
        parts = [self.role, " ".join(self.keywords), self.location, self.country or "", self.level or ""]
        if self.remote:
            parts.append("remote")
        return " · ".join(p for p in parts if p) or "(all)"


class JobListing(BaseModel):
    title: str
    company: str = ""
    location: str = ""
    country: Country
    salary: str = ""  # human-readable as shown by the source
    salary_min: int | None = None  # yearly, in `currency`, when it can be parsed
    currency: str | None = None
    url: str
    posted_date: date | None = None
    description: str = ""  # plain text, never HTML
    source_site: str
    source_tags: list[str] = Field(default_factory=list)
    remote: bool = False
