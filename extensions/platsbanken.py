"""Platsbanken (Sweden) via the JobTech JobSearch API (keyless).

https://jobsearch.api.jobtechdev.se. Offset pagination is capped at roughly 2,000 rows per query;
we stop well below that (`max_results`) and keep a delay between pages.
"""
from __future__ import annotations

from datetime import datetime

from jobtool.extension import Extension, apply_client_filters, looks_remote, translate_role
from jobtool.models import JobListing, SearchFilters

API = "https://jobsearch.api.jobtechdev.se/search"
PAGE_SIZE = 100
OFFSET_CAP = 2000


class Platsbanken(Extension):
    site_id = "platsbanken"
    name = "Platsbanken (Arbetsförmedlingen / JobTech)"
    country = "SE"
    version = "1.0"
    access_method = "api"
    public_demo_enabled = True
    base_url = "https://jobsearch.api.jobtechdev.se"
    min_delay = 1.0
    max_delay = 2.0
    language = "sv"
    max_results = 300

    def params(self, filters: SearchFilters) -> dict:
        terms = [translate_role(filters.role, self.language), *filters.keywords, filters.location]
        params: dict = {"q": " ".join(t for t in terms if t), "limit": PAGE_SIZE, "sort": "pubdate-desc"}
        if filters.remote:
            params["remote"] = "true"
        return params

    def search(self, filters: SearchFilters) -> list[JobListing]:
        params = self.params(filters)
        jobs: list[JobListing] = []
        offset = 0
        while offset < min(self.max_results, OFFSET_CAP):
            resp = self.http.get(API, params={**params, "offset": offset}, headers={"Accept": "application/json"})
            data = resp.json()
            hits = data.get("hits", [])
            jobs.extend(self.to_listing(h) for h in hits)
            total = (data.get("total") or {}).get("value", 0)
            offset += PAGE_SIZE
            if len(hits) < PAGE_SIZE or offset >= total:
                break
        return apply_client_filters(jobs, filters)

    def to_listing(self, hit: dict) -> JobListing:
        addr = hit.get("workplace_address") or {}
        location = addr.get("city") or addr.get("municipality") or addr.get("region") or ""
        description = (hit.get("description") or {}).get("text") or ""
        posted = hit.get("publication_date")
        # salary_type is only "fixed monthly pay" etc., not an amount, so it is not shown.
        salary = hit.get("salary_description") or ""
        workplace_model = ((hit.get("workplace_model") or {}).get("label") or "")
        return JobListing(
            title=hit.get("headline") or "",
            company=(hit.get("employer") or {}).get("name") or "",
            location=location,
            country="SE",
            salary=salary,
            url=hit.get("webpage_url") or f"https://arbetsformedlingen.se/platsbanken/annonser/{hit['id']}",
            posted_date=datetime.fromisoformat(posted).date() if posted else None,
            description=description,
            source_site=self.site_id,
            remote=looks_remote(workplace_model, hit.get("headline") or ""),
        )
