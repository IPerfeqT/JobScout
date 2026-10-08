"""karriere.at (Austria): server-rendered HTML.

Listing pages use path-style URLs (`/jobs/<keyword-slug>/<location-slug>`). The `?page=` parameter
is ignored by the server, so one query yields the first ~15-20 results. Each detail page carries
schema.org JobPosting JSON-LD, which gives the full description, posting date and salary.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime

from selectolax.lexbor import LexborHTMLParser as HTMLParser

from jobtool.extension import (
    Extension,
    apply_client_filters,
    html_to_text,
    looks_remote,
    slugify,
    translate_role,
)
from jobtool.models import JobListing, SearchFilters

log = logging.getLogger(__name__)

SALARY_RE = re.compile(r"(\d{1,3}(?:\.\d{3})+|\d+)\s*(?:€|EUR)\s*(jährlich|monatlich|brutto)?", re.I)


def parse_salary(text: str) -> tuple[int | None, str | None]:
    """'ab 55.000 € jährlich' -> 55000; 'ab 3.200 € monatlich' -> 44800 (Austria pays 14x)."""
    m = SALARY_RE.search(text or "")
    if not m:
        return None, None
    amount = int(m.group(1).replace(".", ""))
    period = (m.group(2) or "").lower()
    if period == "monatlich" or (not period and amount < 15000):
        amount *= 14
    return amount, "EUR"


class KarriereAt(Extension):
    site_id = "karriere_at"
    name = "karriere.at"
    country = "AT"
    version = "1.0"
    access_method = "html"
    public_demo_enabled = True
    base_url = "https://www.karriere.at"
    min_delay = 2.0
    max_delay = 5.0
    language = "de"
    max_details = 20  # detail pages fetched per search (one request each)

    def search_url(self, filters: SearchFilters) -> str:
        terms = [*filters.keywords, translate_role(filters.role, self.language)]
        keyword = slugify(" ".join(t for t in terms if t))
        path = "/jobs"
        if keyword:
            path += f"/{keyword}"
        if filters.location:
            path += f"/{slugify(filters.location)}"
        return self.base_url + path

    def search(self, filters: SearchFilters) -> list[JobListing]:
        resp = self.http.get(self.search_url(filters))
        if resp is None:
            return []
        jobs = self.parse_list(resp.text)
        for job in jobs[: self.max_details]:
            try:
                detail = self.http.get(job.url)
            except Exception as exc:
                log.warning("[%s] detail page failed for %s: %s", self.site_id, job.url, exc)
                continue
            if detail is not None:
                self.enrich_from_detail(job, detail.text)
        return apply_client_filters(jobs, filters)

    def parse_list(self, html: str) -> list[JobListing]:
        tree = HTMLParser(html)
        out: list[JobListing] = []
        for item in tree.css(".m-jobsList__item .m-jobsListItem"):
            link = item.css_first("a.m-jobsListItem__titleLink")
            if link is None:
                continue
            url = link.attributes.get("href", "")
            if url.startswith("/"):
                url = self.base_url + url
            company = item.css_first(".m-jobsListItem__companyName")
            locations = [
                n.text(deep=False).strip() for n in item.css("a.m-jobsListItem__location, span.m-jobsListItem__location")
            ]
            pills = [p.text().strip() for p in item.css(".m-jobsListItem__pill:not(.m-jobsListItem__locations)")]
            salary_text = next((p for p in pills if "€" in p or "EUR" in p), "")
            salary_min, currency = parse_salary(salary_text)
            summary = item.css_first(".m-jobListSummary__text")
            out.append(JobListing(
                title=link.text().strip(),
                company=company.text().strip() if company else "",
                location=", ".join(loc for loc in locations if loc),
                country="AT",
                salary=salary_text,
                salary_min=salary_min,
                currency=currency,
                url=url,
                description=summary.text().strip() if summary else "",
                source_site=self.site_id,
                remote=looks_remote(" ".join(pills)),
            ))
        return out

    def enrich_from_detail(self, job: JobListing, html: str) -> None:
        posting = _find_job_posting(html)
        if not posting:
            return
        desc = html_to_text(posting.get("description", ""))
        if desc:
            job.description = desc
        posted = posting.get("datePosted")
        if posted:
            try:
                job.posted_date = datetime.fromisoformat(posted).date()
            except ValueError:
                pass
        places = posting.get("jobLocation") or []
        if isinstance(places, dict):
            places = [places]
        countries = {(p.get("address") or {}).get("addressCountry") for p in places}
        if countries and countries <= {"DE"}:
            job.country = "DE"
        salary = posting.get("baseSalary") or {}
        value = (salary.get("value") or {}) if isinstance(salary, dict) else {}
        amount = value.get("minValue") or value.get("value")
        if amount and job.salary_min is None:
            unit = (value.get("unitText") or "YEAR").upper()
            job.salary_min = int(amount) * (14 if unit == "MONTH" else 1)
            job.currency = salary.get("currency", "EUR")
            job.salary = job.salary or f"ab {int(amount):,} {job.currency} ({unit.lower()})".replace(",", ".")
        if posting.get("jobLocationType") == "TELECOMMUTE" or looks_remote(job.title):
            job.remote = True


def _find_job_posting(html: str) -> dict | None:
    tree = HTMLParser(html)
    for node in tree.css('script[type="application/ld+json"]'):
        try:
            data = json.loads(node.text())
        except (json.JSONDecodeError, TypeError):
            continue
        for item in data if isinstance(data, list) else [data]:
            if isinstance(item, dict) and item.get("@type") == "JobPosting":
                return item
    return None
