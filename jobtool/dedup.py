"""Duplicate detection: exact URL first, then fuzzy match on title + company + location."""
from __future__ import annotations

import re
from dataclasses import dataclass

from rapidfuzz import fuzz

_GENDER_RE = re.compile(
    r"\((?:[mwfdx]\s*/\s*){1,3}[mwfdx]\*?\)|\(all genders?\)|\(alle\)|\(gn\)|\*in\b|:in\b", re.I
)
_COMPANY_SUFFIX_RE = re.compile(
    r"\b(gmbh|ag|kg|og|e\.?u\.?|co|ab|ltd|inc|se|plc|llc|holding|group|gruppe|austria|sverige|deutschland)\b\.?",
    re.I,
)
_NON_WORD_RE = re.compile(r"[^\w]+", re.UNICODE)

TITLE_THRESHOLD = 90
COMPANY_THRESHOLD = 90
LOCATION_THRESHOLD = 80


def _clean(text: str) -> str:
    return _NON_WORD_RE.sub(" ", text.lower()).strip()


def norm_title(title: str) -> str:
    return _clean(_GENDER_RE.sub(" ", title))


def norm_company(company: str) -> str:
    return _clean(_COMPANY_SUFFIX_RE.sub(" ", company))


def norm_location(location: str) -> str:
    # First place only: "Wien, Linz" and "Wien" should match.
    first = re.split(r"[,;/|]", location or "", maxsplit=1)[0]
    return _clean(first.replace("Vienna", "Wien").replace("Göteborg", "Gothenburg"))


@dataclass(frozen=True)
class NormKey:
    title: str
    company: str
    location: str

    def encode(self) -> str:
        return f"{self.title}|{self.company}|{self.location}"

    @classmethod
    def decode(cls, raw: str) -> "NormKey":
        title, company, location = (raw.split("|") + ["", "", ""])[:3]
        return cls(title, company, location)

    @classmethod
    def of(cls, title: str, company: str, location: str) -> "NormKey":
        return cls(norm_title(title), norm_company(company), norm_location(location))


def is_duplicate(a: NormKey, b: NormKey) -> bool:
    if not a.company or not b.company:
        return False  # too risky to merge without a company
    if fuzz.token_sort_ratio(a.company, b.company) < COMPANY_THRESHOLD:
        return False
    # token_sort (not token_set): "Senior Python Developer" must not merge with "Python Developer".
    if fuzz.token_sort_ratio(a.title, b.title) < TITLE_THRESHOLD:
        return False
    if a.location and b.location and fuzz.ratio(a.location, b.location) < LOCATION_THRESHOLD:
        return False
    return True
