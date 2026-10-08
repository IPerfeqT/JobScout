"""devjobs.at (Austria): disabled.

Its robots.txt allows `/jobs/<tech>` category pages, `/job/<hash>` detail pages and the sitemap,
but every non-robots request currently gets a Cloudflare managed challenge ("Just a moment...").
We do not work around bot protection. Re-enable this once devjobs.at grants crawler access
(e.g. an allowlisted user agent), then implement `search` against the category pages and pull
the site's own tech tags into `source_tags`.
"""
from __future__ import annotations

from jobtool.extension import Extension
from jobtool.models import JobListing, SearchFilters


class DevjobsAt(Extension):
    site_id = "devjobs_at"
    name = "devjobs.at"
    country = "AT"
    version = "0.0"
    access_method = "html"
    public_demo_enabled = True
    enabled = False
    disabled_reason = "Blocked by a Cloudflare bot challenge; waiting for crawler access from devjobs.at."
    base_url = "https://devjobs.at"
    min_delay = 3.0
    max_delay = 6.0
    language = "en"

    def search(self, filters: SearchFilters) -> list[JobListing]:
        raise NotImplementedError(self.disabled_reason)
