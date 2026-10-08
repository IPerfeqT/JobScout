from datetime import date

import pytest

from extensions.karriere_at import KarriereAt, parse_salary
from extensions.platsbanken import Platsbanken
from jobtool.config import Settings
from jobtool.db import Database
from jobtool.dedup import NormKey, is_duplicate
from jobtool.extension import Extension, infer_level, translate_role
from jobtool.models import JobListing, SearchFilters
from jobtool.robots import RobotsRules
from jobtool.scraper import ScraperService
from jobtool.store import JobQuery, JobStore, TransitionError
from jobtool.techstack import default_dictionary


def listing(**kw) -> JobListing:
    base = dict(title="Python Developer (m/w/d)", company="Acme GmbH", location="Wien", country="AT",
                url="https://example.com/1", source_site="test")
    return JobListing(**{**base, **kw})


# --- robots.txt -------------------------------------------------------------------------------

DEVJOBS_ROBOTS = """
User-agent: *
Allow: /
Disallow: /page/
Disallow: /jobs/search?
Disallow: /api/
"""


def test_robots_longest_match_beats_leading_allow():
    rules = RobotsRules(DEVJOBS_ROBOTS)
    ua = "JobAppToolBot/0.1"
    assert rules.can_fetch(ua, "https://devjobs.at/jobs/python")
    assert rules.can_fetch(ua, "https://devjobs.at/job/abc123")
    assert not rules.can_fetch(ua, "https://devjobs.at/jobs/search?q=python")
    assert not rules.can_fetch(ua, "https://devjobs.at/api/jobs")
    assert not rules.can_fetch(ua, "https://devjobs.at/page/crawler")


def test_robots_wildcards_and_specific_agent():
    rules = RobotsRules("User-agent: BadBot\nDisallow: /\n\nUser-agent: *\nDisallow: /*.pdf$\nDisallow:")
    assert not rules.can_fetch("BadBot/1.0", "https://x.at/jobs")
    assert rules.can_fetch("Other", "https://x.at/jobs")
    assert not rules.can_fetch("Other", "https://x.at/files/a.pdf")
    assert rules.can_fetch("Other", "https://x.at/files/a.pdf?x=1")


# --- filters / helpers ------------------------------------------------------------------------

def test_cache_key_is_order_and_case_insensitive():
    a = SearchFilters(role="Developer", keywords="python Django", country="AT")
    b = SearchFilters(role="developer", keywords=["django", "PYTHON"], country="AT")
    assert a.cache_key() == b.cache_key()
    assert a.cache_key() != SearchFilters(role="developer", keywords="python", country="SE").cache_key()


def test_role_translation_and_level():
    assert translate_role("Developer", "de") == "Entwickler"
    assert translate_role("developer", "sv") == "utvecklare"
    assert translate_role("Astronaut", "de") == "Astronaut"
    assert infer_level("Senior Backend Engineer") == "senior"
    assert infer_level("Junior Developer (m/w/d)") == "junior"
    assert infer_level("Lead Developer") == "lead"
    assert infer_level("Software Entwickler") == "mid"


# --- tech extraction --------------------------------------------------------------------------

def test_tech_extraction_aliases_and_boundaries():
    d = default_dictionary()
    text = "Wir suchen Python-Entwickler mit JS, ECMAScript und C++ sowie C#. Erfahrung mit k8s und CI/CD."
    tags = d.extract(text)
    for expected in ["python", "javascript", "cpp", "csharp", "kubernetes", "cicd"]:
        assert expected in tags
    assert "c" not in tags  # "C++" and "C#" must not count as C
    assert "go" not in d.extract("Go-Live im nächsten Quartal, let's go")
    assert "go" in d.extract("Backend in Go and Rust")
    assert "rest" not in d.extract("Der Rest des Teams sitzt in Wien")
    assert "java" not in d.extract("JavaScript only")


def test_source_tags_take_precedence():
    from jobtool.techstack import tech_tags_for

    assert tech_tags_for("Dev", "python java", ["React", "Node.js"]) == ["nodejs", "react"]


# --- dedup ------------------------------------------------------------------------------------

def test_dedup_matching():
    a = NormKey.of("Python Developer (m/w/d)", "Acme GmbH", "Wien")
    assert is_duplicate(a, NormKey.of("Python Developer (w/m/x)", "ACME", "Wien, Linz"))
    assert not is_duplicate(a, NormKey.of("Senior Python Developer (m/w/d)", "Acme GmbH", "Wien"))
    assert not is_duplicate(a, NormKey.of("Python Developer", "Other Corp", "Wien"))
    assert not is_duplicate(a, NormKey.of("Python Developer", "Acme GmbH", "Graz"))


@pytest.fixture
def store():
    return JobStore(Database(":memory:"))


def test_persist_dedups_by_url_then_fuzzy(store):
    r = store.persist([
        listing(),
        listing(url="https://example.com/1", salary="ab 50.000 €"),  # same URL
        listing(url="https://other.se/9", title="Python Developer (w/m/d)", company="ACME", source_site="other"),
        listing(url="https://example.com/2", title="Java Developer"),
    ])
    assert (r.inserted, r.updated, r.merged_duplicates) == (2, 1, 1)
    jobs, total = store.list_jobs("local", JobQuery())
    assert total == 2
    py = next(j for j in jobs if j["title"].startswith("Python"))
    assert py["sources"] == ["other", "test"]


# --- CRM lifecycle and sandbox ----------------------------------------------------------------

def test_lifecycle_transitions(store):
    job_id = store.persist([listing()]).job_ids[0]
    assert store.set_flag("local", job_id, "approve") == ("approve", "approved")
    assert store.set_status("local", job_id, "applied")[1] == "applied"
    with pytest.raises(TransitionError):
        store.set_status("local", job_id, "offer")
    store.set_status("local", job_id, "interviewing")
    assert store.set_status("local", job_id, "offer")[1] == "offer"
    # Flagging later doesn't rewind real progress.
    assert store.set_flag("local", job_id, "unsure") == ("unsure", "offer")


def test_unsure_stays_visible_declined_hidden(store):
    ids = store.persist([listing(), listing(url="https://example.com/2", title="Java Dev")]).job_ids
    store.set_flag("local", ids[0], "unsure")
    store.set_flag("local", ids[1], "decline")
    visible, _ = store.list_jobs("local", JobQuery())
    assert [j["id"] for j in visible] == [ids[0]]
    everything, _ = store.list_jobs("local", JobQuery(show_declined=True))
    assert len(everything) == 2


def test_owner_state_is_isolated(store):
    job_id = store.persist([listing()]).job_ids[0]
    store.set_flag("demo:alice", job_id, "approve")
    assert store.get_state("demo:alice", job_id) == ("approve", "approved")
    assert store.get_state("demo:bob", job_id) == (None, "new")
    assert store.list_jobs("demo:bob", JobQuery(status="approved"))[1] == 0


def test_salary_filter_converts_currency(store):
    store.persist([
        listing(salary_min=50000, currency="EUR"),
        listing(url="https://x.se/1", title="Utvecklare", company="AB X", country="SE",
                salary_min=600000, currency="SEK"),
    ])
    _, total = store.list_jobs("local", JobQuery(min_salary_eur=51000))
    assert total == 1  # 600k SEK is ~52k EUR


# --- extensions -------------------------------------------------------------------------------

KARRIERE_LIST = """
<ol class="m-jobsList"><li class="m-jobsList__item"><div class="m-jobsListItem">
<h2 class="m-jobsListItem__title"><a class="m-jobsListItem__titleLink" href="https://www.karriere.at/jobs/123">Python Dev (m/w/d)</a></h2>
<div class="m-jobsListItem__company"><a class="m-jobsListItem__companyName">Acme GmbH</a></div>
<span class="m-jobsListItem__locations m-jobsListItem__pill"><a class="m-jobsListItem__location">Wien<span class="m-jobsListItem__location--lastComma">, </span></a><a class="m-jobsListItem__location">Graz</a></span>
<span class="m-jobsListItem__pill">Vollzeit</span><span class="m-jobsListItem__pill"> Homeoffice </span><span class="m-jobsListItem__pill">ab 3.500 € monatlich</span>
<p class="m-jobListSummary__text">Short summary</p>
</div></li></ol>
"""

KARRIERE_DETAIL = """<script type="application/ld+json">{"@type": "JobPosting", "title": "Python Dev",
"description": "<p>Wir nutzen <b>Django</b> &amp; Docker</p>", "datePosted": "2026-10-01T12:00:00+02:00",
"jobLocation": [{"address": {"addressCountry": "AT"}}]}</script>"""


def test_karriere_parsing():
    ext = KarriereAt()
    assert ext.search_url(SearchFilters(role="developer", keywords="python", location="Wien")) == \
        "https://www.karriere.at/jobs/python-entwickler/wien"
    [job] = ext.parse_list(KARRIERE_LIST)
    assert (job.title, job.company, job.location) == ("Python Dev (m/w/d)", "Acme GmbH", "Wien, Graz")
    assert job.salary_min == 49000 and job.remote
    ext.enrich_from_detail(job, KARRIERE_DETAIL)
    assert job.description == "Wir nutzen Django & Docker"
    assert job.posted_date == date(2026, 10, 1)


def test_salary_parsing():
    assert parse_salary("ab 55.000 € jährlich") == (55000, "EUR")
    assert parse_salary("ab 3.200 € monatlich") == (44800, "EUR")
    assert parse_salary("Vollzeit") == (None, None)


def test_platsbanken_mapping():
    ext = Platsbanken()
    assert ext.params(SearchFilters(role="developer", keywords="python", location="Stockholm", remote=True)) == {
        "q": "utvecklare python Stockholm", "limit": 100, "sort": "pubdate-desc", "remote": "true"}
    job = ext.to_listing({
        "id": "1", "headline": "Backendutvecklare", "employer": {"name": "AB"}, "webpage_url": "https://af.se/1",
        "workplace_address": {"municipality": "Malmö"}, "publication_date": "2026-10-02T10:00:00",
        "description": {"text": "Python och Kafka"}, "workplace_model": {"label": "Distans"},
        "salary_type": {"label": "Fast lön"},
    })
    assert (job.location, job.posted_date, job.salary, job.remote) == ("Malmö", date(2026, 10, 2), "", True)


# --- scraper service: isolation and cache -----------------------------------------------------

class GoodExt(Extension):
    site_id, name, country, access_method, base_url = "good", "Good", "AT", "api", "https://good.test"
    public_demo_enabled = True
    calls = 0

    def search(self, filters):
        GoodExt.calls += 1
        return [listing(source_site="good")]


class BrokenExt(Extension):
    site_id, name, country, access_method, base_url = "broken", "Broken", "AT", "api", "https://broken.test"
    public_demo_enabled = True

    def search(self, filters):
        raise RuntimeError("layout changed")


class PrivateExt(GoodExt):
    site_id, public_demo_enabled = "private", False


class RecordingNotifier:
    def __init__(self):
        self.failures = []

    def extension_failed(self, site_id, error):
        self.failures.append(site_id)


def make_service(mode="private"):
    notifier = RecordingNotifier()
    svc = ScraperService(Settings(mode=mode), Database(":memory:"), notifier=notifier,
                         extensions={"good": GoodExt, "broken": BrokenExt, "private": PrivateExt})
    return svc, notifier


def test_failing_extension_is_isolated_and_reported():
    svc, notifier = make_service()
    result = svc.search(SearchFilters(country="AT"), only=["good", "broken"])
    outcomes = {o.site_id: o for o in result.outcomes}
    assert outcomes["good"].ok and not outcomes["broken"].ok
    assert "layout changed" in outcomes["broken"].error
    assert notifier.failures == ["broken"]
    assert result.persisted.inserted == 1


def test_cache_serves_second_search():
    svc, _ = make_service()
    GoodExt.calls = 0
    svc.search(SearchFilters(country="AT"), only=["good"])
    second = svc.search(SearchFilters(country="AT"), only=["good"])
    assert GoodExt.calls == 1 and second.outcomes[0].from_cache
    svc.search(SearchFilters(country="AT"), only=["good"], use_cache=False)
    assert GoodExt.calls == 2


def test_demo_mode_runs_only_public_extensions_and_country_filter():
    svc, _ = make_service(mode="demo")
    assert set(svc.extensions) == {"good", "broken"}
    assert svc.search(SearchFilters(country="SE")).outcomes == []
