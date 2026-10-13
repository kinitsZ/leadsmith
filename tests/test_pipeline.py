from sqlalchemy import select

from leadsmith.analyzer import AnalysisError, FakeAnalyzer
from leadsmith.db import Lead, LeadStatus
from leadsmith.pipeline import add_lead, enrich_pending, export_csv, import_csv


def test_import_csv_dedupes_by_normalized_website(session_factory, leads_csv):
    with session_factory() as session:
        first = import_csv(session, leads_csv)
        second = import_csv(session, leads_csv)
        assert (first.added, first.skipped) == (5, 0)
        assert (second.added, second.skipped) == (0, 5)
        # "brightsmiledental.test" was stored in canonical form.
        assert add_lead(session, "Dupe", "HTTPS://BrightSmileDental.test/") is None


def test_import_csv_reports_bad_rows(session_factory, tmp_path):
    path = tmp_path / "leads.csv"
    path.write_text("company,website\nGood Co,good.test\n,missing-company.test\n")
    with session_factory() as session:
        result = import_csv(session, path)
    assert result.added == 1
    assert result.errors == ["line 3: company and website are required"]


async def test_enrich_pending_saves_results_and_failures(session_factory, leads_csv, profile, http):
    with session_factory() as session:
        import_csv(session, leads_csv)

    result = await enrich_pending(session_factory, FakeAnalyzer(), profile, http, concurrency=2)

    assert (result.enriched, result.failed) == (4, 1)
    with session_factory() as session:
        dental = session.scalar(select(Lead).where(Lead.company.like("BrightSmile%")))
        assert dental.status == LeadStatus.ENRICHED
        assert dental.fit_score >= 75
        assert dental.email_body.startswith("Hi Dana,")
        assert dental.emails_found == ["frontdesk@brightsmiledental.test"]

        plumbing = session.scalar(select(Lead).where(Lead.company == "Old Mill Plumbing"))
        assert plumbing.status == LeadStatus.FAILED
        assert "404" in plumbing.error


async def test_enrich_pending_only_retries_failed_when_asked(session_factory, profile, http):
    with session_factory() as session:
        add_lead(session, "Gone", "gone.test")
        session.commit()

    await enrich_pending(session_factory, FakeAnalyzer(), profile, http)
    again = await enrich_pending(session_factory, FakeAnalyzer(), profile, http)
    retried = await enrich_pending(
        session_factory, FakeAnalyzer(), profile, http, retry_failed=True
    )

    assert again.failed == 0
    assert retried.failed == 1


async def test_analysis_errors_mark_lead_failed(session_factory, profile, http):
    class BrokenAnalyzer:
        async def analyze(self, *args):
            raise AnalysisError("model unavailable")

    with session_factory() as session:
        add_lead(session, "Bakery", "greenleafbakery.test")
        session.commit()

    result = await enrich_pending(session_factory, BrokenAnalyzer(), profile, http)

    assert result.failed == 1
    with session_factory() as session:
        assert session.scalar(select(Lead.error)) == "model unavailable"


async def test_export_csv_filters_by_score(session_factory, leads_csv, profile, http, tmp_path):
    with session_factory() as session:
        import_csv(session, leads_csv)
    await enrich_pending(session_factory, FakeAnalyzer(), profile, http)

    with session_factory() as session:
        assert export_csv(session, tmp_path / "all.csv") == 5
        assert export_csv(session, tmp_path / "hot.csv", min_score=75) == 1
    assert "BrightSmile" in (tmp_path / "hot.csv").read_text()
