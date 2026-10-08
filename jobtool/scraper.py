"""Core scraper service: fan out unified filters to the enabled extensions, cache-first."""
from __future__ import annotations

import json
import logging
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from .config import Settings
from .db import Database
from .extension import Extension, PoliteClient
from .models import JobListing, SearchFilters
from .notify import Notifier
from .registry import discover, runnable
from .robots import RobotsChecker
from .store import JobStore, PersistResult

log = logging.getLogger(__name__)


@dataclass
class ExtensionOutcome:
    site_id: str
    ok: bool
    from_cache: bool = False
    count: int = 0
    robots_skipped: int = 0
    error: str | None = None


@dataclass
class ScrapeResult:
    filters: SearchFilters
    outcomes: list[ExtensionOutcome] = field(default_factory=list)
    persisted: PersistResult = field(default_factory=PersistResult)


class ScraperService:
    def __init__(self, settings: Settings, db: Database, notifier: Notifier | None = None,
                 extensions: dict[str, type[Extension]] | None = None):
        self.settings = settings
        self.db = db
        self.store = JobStore(db)
        self.notifier = notifier or Notifier(settings.discord_webhook_url)
        self.robots = RobotsChecker(settings.user_agent)
        self.all_extensions = extensions if extensions is not None else discover()
        self.extensions = runnable(self.all_extensions, demo=settings.is_demo)

    def _run_one(self, ext_cls: type[Extension], filters: SearchFilters, use_cache: bool
                 ) -> tuple[ExtensionOutcome, list[JobListing]]:
        site_id = ext_cls.site_id
        fhash = filters.cache_key()
        fjson = filters.model_dump_json()
        if use_cache:
            cached = self.db.cache_get(site_id, fhash, self.settings.cache_ttl_min)
            if cached is not None:
                listings = [JobListing.model_validate(item) for item in cached]
                run_id = self.db.run_start(site_id, fjson)
                self.db.run_finish(run_id, ok=True, n=len(listings), from_cache=True)
                return ExtensionOutcome(site_id, ok=True, from_cache=True, count=len(listings)), listings

        run_id = self.db.run_start(site_id, fjson)
        http = PoliteClient(ext_cls, self.settings.user_agent, self.robots)
        try:
            listings = ext_cls(http).search(filters)
        except Exception as exc:  # isolate failures: log, alert, carry on with the others
            error = f"{type(exc).__name__}: {exc}"
            log.debug(traceback.format_exc())
            self.db.run_finish(run_id, ok=False, n=0, robots_skipped=len(http.skipped), error=error)
            self.notifier.extension_failed(site_id, error)
            return ExtensionOutcome(site_id, ok=False, robots_skipped=len(http.skipped), error=error), []
        finally:
            http.close()
        self.db.cache_put(site_id, fhash, fjson, [j.model_dump(mode="json") for j in listings])
        self.db.run_finish(run_id, ok=True, n=len(listings), robots_skipped=len(http.skipped))
        return ExtensionOutcome(site_id, ok=True, count=len(listings), robots_skipped=len(http.skipped)), listings

    def search(self, filters: SearchFilters, use_cache: bool = True, only: list[str] | None = None) -> ScrapeResult:
        targets = [
            ext for sid, ext in self.extensions.items()
            if ext.handles(filters) and (only is None or sid in only)
        ]
        result = ScrapeResult(filters)
        if not targets:
            return result
        # Each extension keeps its own delays, so running them side by side is still polite per site.
        with ThreadPoolExecutor(max_workers=len(targets)) as pool:
            futures = [pool.submit(self._run_one, ext, filters, use_cache) for ext in targets]
            all_listings: list[JobListing] = []
            for fut in futures:
                outcome, listings = fut.result()
                result.outcomes.append(outcome)
                all_listings.extend(listings)
        result.persisted = self.store.persist(all_listings)
        log.info(
            "Search %s: %s | inserted=%d updated=%d merged=%d",
            filters.describe(),
            ", ".join(f"{o.site_id}={'cache' if o.from_cache else o.count if o.ok else 'FAILED'}" for o in result.outcomes),
            result.persisted.inserted, result.persisted.updated, result.persisted.merged_duplicates,
        )
        return result

    def refresh_presets(self) -> list[ScrapeResult]:
        presets = json.loads(self.settings.presets_path.read_text(encoding="utf-8"))
        return [self.search(SearchFilters.model_validate(p)) for p in presets]
