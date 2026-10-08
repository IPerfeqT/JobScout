"""SQLite persistence: listings, per-owner CRM state, result cache and extension run log.

Listings are shared. CRM state (flag + lifecycle status) is keyed by an owner: "local" on the
private server, or a random per-visitor session id in the public demo, so visitors get a sandbox.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY,
    url TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    company TEXT NOT NULL DEFAULT '',
    location TEXT NOT NULL DEFAULT '',
    country TEXT NOT NULL,
    salary TEXT NOT NULL DEFAULT '',
    salary_min INTEGER,
    currency TEXT,
    salary_eur INTEGER,  -- salary_min converted at a fixed rate, only for filtering/sorting
    posted_date TEXT,
    description TEXT NOT NULL DEFAULT '',
    source_site TEXT NOT NULL,
    source_tags TEXT NOT NULL DEFAULT '[]',
    tech_tags TEXT NOT NULL DEFAULT '[]',
    remote INTEGER NOT NULL DEFAULT 0,
    level TEXT NOT NULL DEFAULT 'mid',
    norm_key TEXT NOT NULL DEFAULT '',
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_jobs_country ON jobs(country);
CREATE INDEX IF NOT EXISTS idx_jobs_posted ON jobs(posted_date);

-- Every URL a job was seen under (duplicates across sites point at one canonical job).
CREATE TABLE IF NOT EXISTS job_urls (
    url TEXT PRIMARY KEY,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    source_site TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS owners (
    owner TEXT PRIMARY KEY,
    last_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS job_state (
    owner TEXT NOT NULL REFERENCES owners(owner) ON DELETE CASCADE,
    job_id INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    flag TEXT,
    status TEXT NOT NULL DEFAULT 'new',
    updated_at TEXT NOT NULL,
    PRIMARY KEY (owner, job_id)
);

CREATE TABLE IF NOT EXISTS result_cache (
    site_id TEXT NOT NULL,
    filter_hash TEXT NOT NULL,
    filters TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    payload TEXT NOT NULL,
    PRIMARY KEY (site_id, filter_hash)
);

CREATE TABLE IF NOT EXISTS extension_runs (
    id INTEGER PRIMARY KEY,
    site_id TEXT NOT NULL,
    filters TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    ok INTEGER,
    from_cache INTEGER NOT NULL DEFAULT 0,
    n_results INTEGER NOT NULL DEFAULT 0,
    n_robots_skipped INTEGER NOT NULL DEFAULT 0,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_runs_site ON extension_runs(site_id, started_at);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class Database:
    def __init__(self, path: Path | str):
        self.path = Path(path)
        if str(self.path) != ":memory:":
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._memory_conn = sqlite3.connect(":memory:", check_same_thread=False) if str(path) == ":memory:" else None
        with self.connect() as conn:
            conn.executescript(SCHEMA)

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        if self._memory_conn is not None:
            conn = self._memory_conn
        else:
            conn = sqlite3.connect(self.path, timeout=30)
            conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            if self._memory_conn is None:
                conn.close()

    # --- result cache ---------------------------------------------------------------------

    def cache_get(self, site_id: str, filter_hash: str, ttl_min: int) -> list[dict] | None:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT fetched_at, payload FROM result_cache WHERE site_id=? AND filter_hash=?",
                (site_id, filter_hash),
            ).fetchone()
        if not row:
            return None
        age = datetime.now(timezone.utc) - datetime.fromisoformat(row["fetched_at"])
        if age > timedelta(minutes=ttl_min):
            return None
        return json.loads(row["payload"])

    def cache_put(self, site_id: str, filter_hash: str, filters_json: str, payload: list[dict]) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT OR REPLACE INTO result_cache VALUES (?,?,?,?,?)",
                (site_id, filter_hash, filters_json, now_iso(), json.dumps(payload, default=str)),
            )

    # --- extension runs -------------------------------------------------------------------

    def run_start(self, site_id: str, filters_json: str) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO extension_runs (site_id, filters, started_at) VALUES (?,?,?)",
                (site_id, filters_json, now_iso()),
            )
            return cur.lastrowid

    def run_finish(self, run_id: int, ok: bool, n: int, from_cache: bool = False,
                   robots_skipped: int = 0, error: str | None = None) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE extension_runs SET finished_at=?, ok=?, n_results=?, from_cache=?, "
                "n_robots_skipped=?, error=? WHERE id=?",
                (now_iso(), int(ok), n, int(from_cache), robots_skipped, error, run_id),
            )

    def extension_health(self) -> dict[str, sqlite3.Row]:
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT r.* FROM extension_runs r JOIN ("
                "  SELECT site_id, MAX(id) AS id FROM extension_runs WHERE from_cache=0 GROUP BY site_id"
                ") last ON last.id = r.id"
            ).fetchall()
        return {r["site_id"]: r for r in rows}

    # --- owners / sandbox -----------------------------------------------------------------

    def touch_owner(self, owner: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO owners VALUES (?, ?) ON CONFLICT(owner) DO UPDATE SET last_seen=excluded.last_seen",
                (owner, now_iso()),
            )

    def prune_owners(self, older_than_days: int, keep: str = "local") -> int:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=older_than_days)).isoformat()
        with self.connect() as conn:
            cur = conn.execute("DELETE FROM owners WHERE last_seen < ? AND owner != ?", (cutoff, keep))
            return cur.rowcount
