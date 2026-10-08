"""Tech-stack extraction: source tags when a site provides them, otherwise a regex dictionary.

Tech names are mostly language-independent, so one dictionary covers German, Swedish and English
listings. Case-sensitive aliases (e.g. "Go", "REST") must not be followed by a hyphen, which keeps
German compounds like "Go-Live" from matching.
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path

SKILLS_PATH = Path(__file__).with_name("skills.json")


class TechDictionary:
    def __init__(self, data: dict):
        self.labels: dict[str, str] = {}
        self.categories: dict[str, str] = {}
        self._patterns: list[tuple[str, re.Pattern]] = []
        self._alias_to_tag: dict[str, str] = {}
        for tag, spec in data.items():
            if tag.startswith("_"):
                continue
            self.labels[tag] = spec["label"]
            self.categories[tag] = spec.get("category", "other")
            aliases = spec.get("aliases", [])
            for alias in [*aliases, spec["label"], tag]:
                self._alias_to_tag[alias.lower()] = tag
            parts = []
            if aliases:
                alts = "|".join(re.escape(a) for a in sorted(aliases, key=len, reverse=True))
                parts.append(f"(?i:(?<![\\w+#.])(?:{alts})(?![\\w+#]))")
            cs = spec.get("case_sensitive", [])
            if cs:
                alts = "|".join(re.escape(a) for a in sorted(cs, key=len, reverse=True))
                parts.append(f"(?<![\\w+#.\\-])(?:{alts})(?![\\w+#\\-])")
            if parts:
                self._patterns.append((tag, re.compile("|".join(parts))))

    def extract(self, text: str) -> list[str]:
        if not text:
            return []
        return sorted(tag for tag, pattern in self._patterns if pattern.search(text))

    def normalize_source_tags(self, tags: list[str]) -> list[str]:
        """Map a site's own tags onto canonical tags; unknown tags are kept as lowercase slugs."""
        out = set()
        for t in tags:
            key = t.strip().lower()
            if key:
                out.add(self._alias_to_tag.get(key, key))
        return sorted(out)

    def label(self, tag: str) -> str:
        return self.labels.get(tag, tag)


@lru_cache(maxsize=1)
def default_dictionary() -> TechDictionary:
    return TechDictionary(json.loads(SKILLS_PATH.read_text(encoding="utf-8")))


def tech_tags_for(title: str, description: str, source_tags: list[str]) -> list[str]:
    d = default_dictionary()
    if source_tags:
        return d.normalize_source_tags(source_tags)
    return d.extract(f"{title}\n{description}")
