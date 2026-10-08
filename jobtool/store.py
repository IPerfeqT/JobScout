"""Job persistence (with dedup), CRM state transitions and dashboard queries."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, timedelta

from .db import Database, now_iso
from .dedup import NormKey, is_duplicate
from .extension import infer_level
from .models import JobListing
from .techstack import tech_tags_for

STATUSES = ["new", "approved", "applied", "interviewing", "offer", "rejected"]
FLAGS = ["approve", "unsure", "decline"]

# Manual lifecycle transitions only. Each step also allows going back one step to fix mistakes.
TRANSITIONS: dict[str, list[str]] = {
    "new": ["approved"],
    "approved": ["applied", "new"],
    "applied": ["interviewing", "rejected", "approved"],
    "interviewing": ["offer", "rejected", "applied"],
    "offer": ["interviewing"],
    "rejected": ["applied", "interviewing"],
}

# Rough fixed rates; only used to make salary filters comparable across countries.
EUR_RATES = {"EUR": 1.0, "SEK": 0.087}


class TransitionError(ValueError):
    pass


@dataclass
class PersistResult:
    inserted: int = 0
    updated: int = 0
    merged_duplicates: int = 0
    job_ids: list[int] = field(default_factory=list)


@dataclass
class JobQuery:
    status: str | None = None
    flag: str | None = None  # a flag, "none" (unflagged) or None (any)
    show_declined: bool = False
    country: str | None = None
    source: str | None = None
    min_salary_eur: int | None = None
    posted_within_days: int | None = None
    text: str = ""
    tech: str | None = None
    sort: str = "posted"  # posted | salary | seen
    limit: int = 50
    offset: int = 0


def _salary_eur(listing: JobListing) -> int | None:
    if listing.salary_min is None:
        return None
    rate = EUR_RATES.get((listing.currency or "EUR").upper())
    return round(listing.salary_min * rate) if rate else None


class JobStore:
    def __init__(self, db: Database):
        self.db = db

    # --- persistence ------------------------------------------------------------------------

    def persist(self, listings: list[JobListing]) -> PersistResult:
        result = PersistResult()
        now = now_iso()
        with self.db.connect() as conn:
            for listing in listings:
                key = NormKey.of(listing.title, listing.company, listing.location)
                tags = tech_tags_for(listing.title, listing.description, listing.source_tags)
                row = conn.execute("SELECT job_id FROM job_urls WHERE url=?", (listing.url,)).fetchone()
                if row:
                    job_id = row["job_id"]
                    conn.execute(
                        "UPDATE jobs SET title=?, company=?, location=?, salary=COALESCE(NULLIF(?, ''), salary), "
                        "salary_min=COALESCE(?, salary_min), currency=COALESCE(?, currency), "
                        "salary_eur=COALESCE(?, salary_eur), posted_date=COALESCE(?, posted_date), "
                        "description=CASE WHEN length(?) > length(description) THEN ? ELSE description END, "
                        "source_tags=?, tech_tags=?, remote=?, level=?, norm_key=?, last_seen=? "
                        "WHERE id=? AND url=?",
                        (listing.title, listing.company, listing.location, listing.salary, listing.salary_min,
                         listing.currency, _salary_eur(listing), _iso(listing.posted_date),
                         listing.description, listing.description, json.dumps(listing.source_tags),
                         json.dumps(tags), int(listing.remote), infer_level(listing.title), key.encode(),
                         now, job_id, listing.url),
                    )
                    conn.execute("UPDATE jobs SET last_seen=? WHERE id=?", (now, job_id))
                    result.updated += 1
                    result.job_ids.append(job_id)
                    continue

                dup_id = self._find_fuzzy_duplicate(conn, listing.country, key)
                if dup_id is not None:
                    conn.execute(
                        "INSERT INTO job_urls VALUES (?,?,?)", (listing.url, dup_id, listing.source_site)
                    )
                    # Fill gaps on the canonical record from the duplicate.
                    conn.execute(
                        "UPDATE jobs SET last_seen=?, "
                        "salary=CASE WHEN salary='' THEN ? ELSE salary END, "
                        "salary_min=COALESCE(salary_min, ?), currency=COALESCE(currency, ?), "
                        "salary_eur=COALESCE(salary_eur, ?), "
                        "description=CASE WHEN description='' THEN ? ELSE description END WHERE id=?",
                        (now, listing.salary, listing.salary_min, listing.currency, _salary_eur(listing),
                         listing.description, dup_id),
                    )
                    result.merged_duplicates += 1
                    result.job_ids.append(dup_id)
                    continue

                cur = conn.execute(
                    "INSERT INTO jobs (url, title, company, location, country, salary, salary_min, currency, "
                    "salary_eur, posted_date, description, source_site, source_tags, tech_tags, remote, level, "
                    "norm_key, first_seen, last_seen) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (listing.url, listing.title, listing.company, listing.location, listing.country,
                     listing.salary, listing.salary_min, listing.currency, _salary_eur(listing),
                     _iso(listing.posted_date), listing.description, listing.source_site,
                     json.dumps(listing.source_tags), json.dumps(tags), int(listing.remote),
                     infer_level(listing.title), key.encode(), now, now),
                )
                job_id = cur.lastrowid
                conn.execute("INSERT INTO job_urls VALUES (?,?,?)", (listing.url, job_id, listing.source_site))
                result.inserted += 1
                result.job_ids.append(job_id)
        return result

    @staticmethod
    def _find_fuzzy_duplicate(conn, country: str, key: NormKey) -> int | None:
        if not key.company:
            return None
        cutoff = (date.today() - timedelta(days=120)).isoformat()
        rows = conn.execute(
            "SELECT id, norm_key FROM jobs WHERE country=? AND last_seen >= ?", (country, cutoff)
        ).fetchall()
        for r in rows:
            if is_duplicate(key, NormKey.decode(r["norm_key"])):
                return r["id"]
        return None

    def retag_all(self) -> int:
        with self.db.connect() as conn:
            rows = conn.execute("SELECT id, title, description, source_tags FROM jobs").fetchall()
            for r in rows:
                tags = tech_tags_for(r["title"], r["description"], json.loads(r["source_tags"]))
                conn.execute("UPDATE jobs SET tech_tags=? WHERE id=?", (json.dumps(tags), r["id"]))
        return len(rows)

    # --- CRM state --------------------------------------------------------------------------

    def get_state(self, owner: str, job_id: int) -> tuple[str | None, str]:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT flag, status FROM job_state WHERE owner=? AND job_id=?", (owner, job_id)
            ).fetchone()
        return (row["flag"], row["status"]) if row else (None, "new")

    def _save_state(self, owner: str, job_id: int, flag: str | None, status: str) -> None:
        self.db.touch_owner(owner)
        with self.db.connect() as conn:
            if not conn.execute("SELECT 1 FROM jobs WHERE id=?", (job_id,)).fetchone():
                raise KeyError(job_id)
            conn.execute(
                "INSERT INTO job_state VALUES (?,?,?,?,?) ON CONFLICT(owner, job_id) DO UPDATE SET "
                "flag=excluded.flag, status=excluded.status, updated_at=excluded.updated_at",
                (owner, job_id, flag, status, now_iso()),
            )

    def set_flag(self, owner: str, job_id: int, flag: str | None) -> tuple[str | None, str]:
        if flag is not None and flag not in FLAGS:
            raise TransitionError(f"Unknown flag {flag!r}")
        _, status = self.get_state(owner, job_id)
        if flag == "approve" and status == "new":
            status = "approved"
        elif flag in ("decline", "unsure", None) and status == "approved":
            status = "new"  # un-approving only undoes triage, never later lifecycle steps
        self._save_state(owner, job_id, flag, status)
        return flag, status

    def set_status(self, owner: str, job_id: int, new_status: str) -> tuple[str | None, str]:
        flag, status = self.get_state(owner, job_id)
        if new_status not in TRANSITIONS.get(status, []):
            raise TransitionError(f"Cannot move from {status!r} to {new_status!r}")
        if new_status == "approved":
            flag = "approve"
        elif new_status == "new" and flag == "approve":
            flag = None
        self._save_state(owner, job_id, flag, new_status)
        return flag, new_status

    # --- queries ----------------------------------------------------------------------------

    def _where(self, q: JobQuery, owner: str) -> tuple[str, list]:
        clauses, params = [], []
        status_expr = "COALESCE(s.status, 'new')"
        if q.status:
            clauses.append(f"{status_expr} = ?")
            params.append(q.status)
        if q.flag == "none":
            clauses.append("s.flag IS NULL")
        elif q.flag:
            clauses.append("s.flag = ?")
            params.append(q.flag)
        elif not q.show_declined:
            clauses.append("(s.flag IS NULL OR s.flag != 'decline')")
        if q.country:
            clauses.append("j.country = ?")
            params.append(q.country)
        if q.source:
            clauses.append("EXISTS (SELECT 1 FROM job_urls u WHERE u.job_id = j.id AND u.source_site = ?)")
            params.append(q.source)
        if q.min_salary_eur:
            clauses.append("j.salary_eur >= ?")
            params.append(q.min_salary_eur)
        if q.posted_within_days:
            clauses.append("COALESCE(j.posted_date, substr(j.first_seen, 1, 10)) >= ?")
            params.append((date.today() - timedelta(days=q.posted_within_days)).isoformat())
        if q.text:
            for word in q.text.split():
                clauses.append("(j.title LIKE ? OR j.company LIKE ? OR j.location LIKE ?)")
                params += [f"%{word}%"] * 3
        if q.tech:
            clauses.append("EXISTS (SELECT 1 FROM json_each(j.tech_tags) t WHERE t.value = ?)")
            params.append(q.tech)
        where = " AND ".join(clauses) or "1=1"
        return where, params

    def list_jobs(self, owner: str, q: JobQuery) -> tuple[list[dict], int]:
        where, params = self._where(q, owner)
        order = {
            "salary": "j.salary_eur IS NULL, j.salary_eur DESC",
            "seen": "j.first_seen DESC",
        }.get(q.sort, "COALESCE(j.posted_date, substr(j.first_seen, 1, 10)) DESC, j.id DESC")
        base = (
            "FROM jobs j LEFT JOIN job_state s ON s.job_id = j.id AND s.owner = ? "
            f"WHERE {where}"
        )
        with self.db.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) {base}", [owner, *params]).fetchone()[0]
            rows = conn.execute(
                f"SELECT j.*, s.flag, COALESCE(s.status, 'new') AS status, "
                f"(SELECT group_concat(u.source_site) FROM job_urls u WHERE u.job_id = j.id) AS sources "
                f"{base} ORDER BY {order} LIMIT ? OFFSET ?",
                [owner, *params, q.limit, q.offset],
            ).fetchall()
        return [_row_to_job(r) for r in rows], total

    def get_job(self, owner: str, job_id: int) -> dict | None:
        with self.db.connect() as conn:
            row = conn.execute(
                "SELECT j.*, s.flag, COALESCE(s.status, 'new') AS status, "
                "(SELECT group_concat(u.source_site) FROM job_urls u WHERE u.job_id = j.id) AS sources "
                "FROM jobs j LEFT JOIN job_state s ON s.job_id = j.id AND s.owner = ? WHERE j.id = ?",
                (owner, job_id),
            ).fetchone()
            if not row:
                return None
            job = _row_to_job(row)
            job["urls"] = [dict(u) for u in conn.execute(
                "SELECT url, source_site FROM job_urls WHERE job_id=? ORDER BY url", (job_id,)
            )]
        return job

    def status_counts(self, owner: str) -> dict[str, int]:
        with self.db.connect() as conn:
            rows = conn.execute(
                "SELECT COALESCE(s.status, 'new') AS status, COUNT(*) AS n FROM jobs j "
                "LEFT JOIN job_state s ON s.job_id = j.id AND s.owner = ? "
                "WHERE s.flag IS NULL OR s.flag != 'decline' GROUP BY 1",
                (owner,),
            ).fetchall()
        counts = {s: 0 for s in STATUSES}
        counts.update({r["status"]: r["n"] for r in rows})
        return counts

    def tech_counts(self, country: str | None = None, source: str | None = None) -> tuple[list[tuple[str, int]], int]:
        clauses, params = [], []
        if country:
            clauses.append("j.country = ?")
            params.append(country)
        if source:
            clauses.append("EXISTS (SELECT 1 FROM job_urls u WHERE u.job_id = j.id AND u.source_site = ?)")
            params.append(source)
        where = " AND ".join(clauses) or "1=1"
        with self.db.connect() as conn:
            total = conn.execute(f"SELECT COUNT(*) FROM jobs j WHERE {where}", params).fetchone()[0]
            rows = conn.execute(
                f"SELECT t.value AS tag, COUNT(*) AS n FROM jobs j, json_each(j.tech_tags) t "
                f"WHERE {where} GROUP BY t.value ORDER BY n DESC, tag",
                params,
            ).fetchall()
        return [(r["tag"], r["n"]) for r in rows], total

    def facets(self) -> dict[str, list[str]]:
        with self.db.connect() as conn:
            countries = [r[0] for r in conn.execute("SELECT DISTINCT country FROM jobs ORDER BY 1")]
            sources = [r[0] for r in conn.execute("SELECT DISTINCT source_site FROM job_urls ORDER BY 1")]
        return {"countries": countries, "sources": sources}


def _iso(d: date | None) -> str | None:
    return d.isoformat() if d else None


def _row_to_job(row) -> dict:
    job = dict(row)
    job["source_tags"] = json.loads(job.get("source_tags") or "[]")
    job["tech_tags"] = json.loads(job.get("tech_tags") or "[]")
    job["sources"] = sorted(set((job.get("sources") or job["source_site"]).split(",")))
    job["remote"] = bool(job["remote"])
    return job
