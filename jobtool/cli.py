"""Command line: scrape, refresh presets, list extensions, retag, serve."""
from __future__ import annotations

import argparse
import logging

from .config import load_settings
from .db import Database


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="jobtool")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("scrape", help="run one search across the enabled extensions")
    s.add_argument("--role", default="")
    s.add_argument("--keywords", default="", help="space or comma separated")
    s.add_argument("--location", default="")
    s.add_argument("--country", choices=["AT", "SE", "DE"])
    s.add_argument("--level", choices=["junior", "mid", "senior", "lead"])
    s.add_argument("--remote", action="store_true")
    s.add_argument("--only", nargs="*", help="limit to these site ids")
    s.add_argument("--no-cache", action="store_true")

    sub.add_parser("refresh", help="run all searches in presets.json (cache-first); use from cron")
    sub.add_parser("extensions", help="list discovered extensions")
    sub.add_parser("retag", help="recompute tech tags for all stored jobs")
    sub.add_parser("prune", help="delete stale demo sandbox sessions")

    srv = sub.add_parser("serve", help="run the dashboard")
    srv.add_argument("--host", default="127.0.0.1")
    srv.add_argument("--port", type=int, default=8000)

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings = load_settings()

    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("jobtool.web.app:create_app", factory=True, host=args.host, port=args.port)
        return

    db = Database(settings.db_path)
    if args.cmd == "extensions":
        from .registry import discover

        for sid, ext in sorted(discover().items()):
            i = ext.info()
            state = "enabled" if i["enabled"] else f"DISABLED ({i['disabled_reason']})"
            demo = "demo" if i["public_demo_enabled"] else "private-only"
            print(f"{sid:14} {i['country']}  {i['access_method']:4}  v{i['version']:5} {demo:12} {state}")
    elif args.cmd == "scrape":
        from .models import SearchFilters
        from .scraper import ScraperService

        filters = SearchFilters(role=args.role, keywords=args.keywords, location=args.location,
                                country=args.country, level=args.level, remote=args.remote)
        result = ScraperService(settings, db).search(filters, use_cache=not args.no_cache, only=args.only)
        for o in result.outcomes:
            status = "cache" if o.from_cache else ("ok" if o.ok else f"FAILED: {o.error}")
            print(f"{o.site_id:14} {o.count:4} listings  {status}  robots-skipped={o.robots_skipped}")
        p = result.persisted
        print(f"inserted={p.inserted} updated={p.updated} merged_duplicates={p.merged_duplicates}")
    elif args.cmd == "refresh":
        from .scraper import ScraperService

        for r in ScraperService(settings, db).refresh_presets():
            print(f"{r.filters.describe():50} +{r.persisted.inserted} new, {r.persisted.merged_duplicates} dup")
        print(f"pruned {db.prune_owners(settings.demo_session_days)} stale demo sessions")
    elif args.cmd == "retag":
        from .store import JobStore

        print(f"retagged {JobStore(db).retag_all()} jobs")
    elif args.cmd == "prune":
        print(f"pruned {db.prune_owners(settings.demo_session_days)} stale demo sessions")


if __name__ == "__main__":
    main()
