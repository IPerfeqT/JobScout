from fastapi.testclient import TestClient

from jobtool.config import Settings
from jobtool.db import Database
from jobtool.scraper import ScraperService
from jobtool.store import JobStore
from jobtool.web.app import create_app

from .test_core import listing


def make_client(mode="private", **settings_kw):
    settings = Settings(mode=mode, **settings_kw)
    db = Database(":memory:")
    job_id = JobStore(db).persist([listing()]).job_ids[0]
    app = create_app(settings, ScraperService(settings, db, extensions={}))
    return TestClient(app), job_id


def test_pages_render():
    client, job_id = make_client()
    for url in ["/", "/analytics", "/sources", f"/jobs/{job_id}", "/?tech=python&country=AT&sort=salary"]:
        assert client.get(url).status_code == 200, url
    assert client.get("/jobs/9999").status_code == 404


def test_demo_visitors_get_separate_sandboxes():
    alice, job_id = make_client(mode="demo")
    r = alice.post(f"/jobs/{job_id}/flag", data={"flag": "approve", "next": "/"}, follow_redirects=False)
    assert r.status_code == 303 and "jt_sid" in r.cookies
    assert "status-approved" in alice.get(f"/jobs/{job_id}").text

    bob = TestClient(alice.app)  # fresh cookie jar = another visitor
    assert "status-approved" not in bob.get(f"/jobs/{job_id}").text
    assert "status-new" in bob.get(f"/jobs/{job_id}").text


def test_demo_blocks_live_search():
    client, _ = make_client(mode="demo")
    assert client.post("/search", data={"role": "developer"}).status_code == 403


def test_redirect_target_must_be_local():
    client, job_id = make_client()
    r = client.post(f"/jobs/{job_id}/flag", data={"flag": "unsure", "next": "//evil.example"}, follow_redirects=False)
    assert r.headers["location"] == "/"


def test_rate_limit():
    client, _ = make_client(rate_limit_read=3)
    codes = [client.get("/healthz").status_code for _ in range(5)]
    assert codes[:3] == [200, 200, 200] and codes[-1] == 429
