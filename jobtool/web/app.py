"""FastAPI dashboard: job list with flags and lifecycle, detail view, analytics, source health.

Private mode: one owner ("local"), and a search form that runs live (cache-first) scrapes.
Demo mode: listings come only from the preset refresh; every visitor gets a sandboxed CRM state
via a random session cookie, and all requests are rate-limited per visitor.
"""
from __future__ import annotations

import logging
import secrets
import threading
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import BackgroundTasks, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..config import Settings, load_settings
from ..db import Database
from ..models import SearchFilters
from ..scraper import ScraperService
from ..store import FLAGS, STATUSES, TRANSITIONS, JobQuery, JobStore, TransitionError
from ..techstack import default_dictionary
from .ratelimit import RateLimiter

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
SESSION_COOKIE = "jt_sid"
PAGE_SIZE = 50


def _safe_next(url: str | None, default: str = "/") -> str:
    # Only same-site relative paths, so the redirect can't be abused.
    if url and url.startswith("/") and not url.startswith("//") and "\\" not in url:
        return url
    return default


def _int_or_none(v: str | None) -> int | None:
    try:
        return int(v) if v not in (None, "") else None
    except ValueError:
        return None


def create_app(settings: Settings | None = None, scraper: ScraperService | None = None) -> FastAPI:
    settings = settings or load_settings()
    db = scraper.db if scraper else Database(settings.db_path)
    scraper = scraper or ScraperService(settings, db)
    store = JobStore(db)
    tech = default_dictionary()
    read_limiter = RateLimiter(settings.rate_limit_read)
    write_limiter = RateLimiter(settings.rate_limit_write)
    search_state = {"running": False, "last": None}
    search_lock = threading.Lock()
    stop_refresh = threading.Event()

    def refresh_loop() -> None:
        while True:
            try:
                scraper.refresh_presets()
                db.prune_owners(settings.demo_session_days)
            except Exception:
                log.exception("Preset refresh failed")
            if stop_refresh.wait(settings.refresh_interval_min * 60):
                return

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.refresh_interval_min > 0:
            threading.Thread(target=refresh_loop, name="preset-refresh", daemon=True).start()
        yield
        stop_refresh.set()

    app = FastAPI(title="Job Application Tool", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(
        settings=settings, tech_label=tech.label, STATUSES=STATUSES, FLAGS=FLAGS, TRANSITIONS=TRANSITIONS,
    )

    def client_key(request: Request) -> str:
        if settings.trust_proxy:
            fwd = request.headers.get("x-forwarded-for")
            if fwd:
                return fwd.split(",")[0].strip()
        return request.client.host if request.client else "unknown"

    @app.middleware("http")
    async def visitor_middleware(request: Request, call_next):
        if not request.url.path.startswith("/static"):
            limiter = read_limiter if request.method in ("GET", "HEAD") else write_limiter
            if not limiter.allow(client_key(request)):
                return PlainTextResponse("Too many requests, slow down a little.", status_code=429,
                                         headers={"Retry-After": "60"})
        new_sid = None
        if settings.is_demo:
            sid = request.cookies.get(SESSION_COOKIE, "")
            if len(sid) < 20:
                sid = new_sid = secrets.token_urlsafe(24)
            request.state.owner = f"demo:{sid}"
        else:
            request.state.owner = "local"
        response = await call_next(request)
        if new_sid:
            response.set_cookie(
                SESSION_COOKIE, new_sid, max_age=settings.demo_session_days * 86400, httponly=True,
                samesite="lax", secure=request.url.scheme == "https",
            )
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    def render(request: Request, name: str, **ctx) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"facets": store.facets(), **ctx})

    @app.get("/", response_class=HTMLResponse)
    def job_list(request: Request):
        p = request.query_params
        page = max(_int_or_none(p.get("page")) or 1, 1)
        q = JobQuery(
            status=p.get("status") if p.get("status") in STATUSES else None,
            flag=p.get("flag") if p.get("flag") in [*FLAGS, "none"] else None,
            show_declined=p.get("show_declined") == "1",
            country=p.get("country") or None,
            source=p.get("source") or None,
            min_salary_eur=_int_or_none(p.get("min_salary")),
            posted_within_days=_int_or_none(p.get("days")),
            text=(p.get("q") or "")[:100],
            tech=p.get("tech") or None,
            sort=p.get("sort") or "posted",
            limit=PAGE_SIZE,
            offset=(page - 1) * PAGE_SIZE,
        )
        jobs, total = store.list_jobs(request.state.owner, q)
        params = {k: v for k, v in p.items() if k != "page" and v}
        return render(
            request, "jobs.html", jobs=jobs, total=total, page=page, pages=max((total - 1) // PAGE_SIZE + 1, 1),
            params=params, query_string=urlencode(params), counts=store.status_counts(request.state.owner),
            current_url=str(request.url.path) + (f"?{request.url.query}" if request.url.query else ""),
            search_state=search_state,
        )

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(request: Request, job_id: int):
        job = store.get_job(request.state.owner, job_id)
        if not job:
            raise HTTPException(404, "Job not found")
        return render(request, "job.html", job=job, back=_safe_next(request.query_params.get("back")),
                      current_url=f"/jobs/{job_id}")

    @app.post("/jobs/{job_id}/flag")
    def set_flag(request: Request, job_id: int, flag: str = Form(""), next: str = Form("/")):
        try:
            store.set_flag(request.state.owner, job_id, flag or None)
        except KeyError:
            raise HTTPException(404, "Job not found")
        except TransitionError as exc:
            raise HTTPException(400, str(exc))
        return RedirectResponse(_safe_next(next), status_code=303)

    @app.post("/jobs/{job_id}/status")
    def set_status(request: Request, job_id: int, status: str = Form(...), next: str = Form("/")):
        try:
            store.set_status(request.state.owner, job_id, status)
        except KeyError:
            raise HTTPException(404, "Job not found")
        except TransitionError as exc:
            raise HTTPException(400, str(exc))
        return RedirectResponse(_safe_next(next), status_code=303)

    @app.get("/analytics", response_class=HTMLResponse)
    def analytics(request: Request):
        country = request.query_params.get("country") or None
        source = request.query_params.get("source") or None
        counts, total = store.tech_counts(country, source)
        return render(request, "analytics.html", counts=counts[:30], total=total, country=country,
                      source=source, categories=tech.categories,
                      max_count=counts[0][1] if counts else 1)

    @app.get("/sources", response_class=HTMLResponse)
    def sources(request: Request):
        health = db.extension_health()
        rows = []
        for sid, ext in sorted(scraper.all_extensions.items()):
            info = ext.info()
            info["active"] = sid in scraper.extensions
            info["last"] = dict(health[sid]) if sid in health else None
            rows.append(info)
        return render(request, "sources.html", extensions=rows)

    @app.post("/search")
    def run_search(
        background: BackgroundTasks,
        role: str = Form(""), keywords: str = Form(""), location: str = Form(""),
        country: str = Form(""), level: str = Form(""), remote: str = Form(""),
    ):
        if settings.is_demo:
            raise HTTPException(403, "Live searches are disabled in the public demo.")
        try:
            filters = SearchFilters(role=role, keywords=keywords, location=location, country=country or None,
                                    level=level or None, remote=remote == "1")
        except ValueError as exc:
            raise HTTPException(400, str(exc))

        def task() -> None:
            with search_lock:
                search_state["running"] = True
                try:
                    r = scraper.search(filters)
                    search_state["last"] = {
                        "filters": filters.describe(),
                        "inserted": r.persisted.inserted,
                        "merged": r.persisted.merged_duplicates,
                        "outcomes": [o.__dict__ for o in r.outcomes],
                    }
                finally:
                    search_state["running"] = False

        background.add_task(task)
        return RedirectResponse("/?sort=seen", status_code=303)

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "mode": settings.mode}

    return app
