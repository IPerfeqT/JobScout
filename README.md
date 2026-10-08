# JobScout

A job aggregator for the Austrian and Swedish job markets, with a CRM-style dashboard for tracking
applications and analytics on which technologies employers ask for.

**Stack:** Python 3.11+, FastAPI, Jinja2, SQLite, httpx, selectolax, RapidFuzz, pytest, Docker

## Features

- **Plugin-based sources.** Each job board is one file in `extensions/`, discovered automatically
  at startup. HTML scrapers and REST API clients share the same interface.
- **Polite scraping.** Robots.txt is checked before every HTML request (longest-match rules,
  wildcards), with randomized per-site delays and a 60-minute result cache per site and search.
- **Fault isolation.** A failing source is logged, shown on the Sources page and (optionally)
  reported to a Discord webhook. The other sources keep running.
- **Deduplication.** Exact URL match first, then fuzzy match on title + company + location, with
  gender markers ("(m/w/d)") and company suffixes ("GmbH", "AB") normalized away.
- **Tech-stack extraction.** Dictionary and regex matching of technologies and their aliases, which
  works for German, Swedish and English ads. A chart shows the most in-demand skills by country and
  source.
- **Application tracking.** Triage jobs as Approve / Unsure / Decline and move them through
  New → Approved → Applied → Interviewing → Offer / Rejected. Filters cover status, salary, date,
  country, source and technology.
- **Public demo mode.** Visitors can't trigger live scraping, because listings come from a scheduled
  refresh. Each visitor gets a private sandbox through a session cookie, and requests are rate-limited.

## Project status

| Source | Country | Access | Status |
|---|---|---|---|
| karriere.at | AT | HTML | Working. The site returns about 15–20 results per search (it ignores pagination). |
| Platsbanken (JobTech API) | SE | REST API | Working |
| devjobs.at | AT | HTML | Disabled. The site blocks automated clients with a bot challenge. |
| StepStone.at | AT | HTML | Planned |
| Bundesagentur für Arbeit | DE | API | Planned |

- Core pipeline, dedup, tech extraction, dashboard and demo mode are done and covered by tests.
- A Dockerfile is included, but the public demo isn't deployed yet.

## Getting started

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate    macOS/Linux: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env          # optional, the defaults work

python -m jobtool extensions  # list the discovered sources
python -m jobtool scrape --role developer --keywords python --country AT
python -m jobtool refresh     # run every search in jobtool/presets.json
python -m jobtool serve       # dashboard at http://127.0.0.1:8000
python -m pytest
```

Other commands: `retag` recomputes tech tags after you edit `jobtool/skills.json`, and `prune`
deletes expired demo sessions.

## Configuration

All settings are environment variables (see `.env.example`). The main ones:

| Variable | Default | Purpose |
|---|---|---|
| `JOBTOOL_MODE` | `private` | `private` allows live searches from the dashboard. `demo` is cache-only, with sandboxes and rate limits. |
| `JOBTOOL_CACHE_TTL_MIN` | `60` | Result cache lifetime per source and search |
| `JOBTOOL_REFRESH_INTERVAL_MIN` | `0` | In-process refresh of the presets (`0` = off, so use cron + `jobtool refresh`) |
| `DISCORD_WEBHOOK_URL` | empty | Alerts when a source breaks |

## Project layout

```
extensions/          one module per job board (auto-discovered)
jobtool/
  extension.py       base class, polite HTTP client, shared helpers
  robots.py          robots.txt parser
  scraper.py         fan-out, cache, failure isolation
  store.py, db.py    SQLite persistence, CRM state, queries
  dedup.py           duplicate detection
  techstack.py       technology extraction (+ skills.json)
  web/               FastAPI app, templates, CSS
tests/
```

### Adding a source

Create `extensions/<site_id>.py` with a subclass of `jobtool.extension.Extension`. Set `site_id`,
`country`, `access_method` (`"html"` or `"api"`) and `base_url`, then implement
`search(filters) -> list[JobListing]`, fetching only through `self.http`. That client applies the
delays and the robots.txt check.

## Deployment

```bash
docker build -t jobscout .
docker run -d -p 8000:8000 -v jobscout-data:/data jobscout
```

The image runs in demo mode and refreshes the presets every 3 hours. Put it behind a TLS reverse
proxy and set `JOBTOOL_TRUST_PROXY=true` so rate limits use the real client IP. The rate limiter is
in-memory, so run a single worker.

## Data sources

Listings belong to their original publishers, and every job links back to the original ad. Swedish
listings come from Arbetsförmedlingen's [JobTech JobSearch API](https://jobsearch.api.jobtechdev.se).
