"""Import leads, enrich them concurrently, and export the results."""

import asyncio
import csv
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from .analyzer import AnalysisError, Analyzer, LeadAnalysis, LeadInput
from .config import AgencyProfile
from .db import Lead, LeadStatus
from .scraper import ScrapeError, SiteSnapshot, fetch_site, normalize_url

EXPORT_COLUMNS = [
    "company",
    "website",
    "contact_name",
    "contact_email",
    "status",
    "fit_score",
    "industry",
    "summary",
    "fit_reasoning",
    "email_subject",
    "email_body",
    "emails_found",
    "error",
]


@dataclass
class ImportResult:
    added: int = 0
    skipped: int = 0
    errors: list[str] = field(default_factory=list)


@dataclass
class EnrichResult:
    enriched: int = 0
    failed: int = 0


def add_lead(
    session: Session,
    company: str,
    website: str,
    contact_name: str | None = None,
    contact_email: str | None = None,
) -> Lead | None:
    """Insert a lead unless one with the same website already exists. Returns None on duplicate."""
    url = normalize_url(website)
    if session.scalar(select(Lead.id).where(Lead.website == url)):
        return None
    lead = Lead(
        company=company.strip(),
        website=url,
        contact_name=(contact_name or "").strip() or None,
        contact_email=(contact_email or "").strip() or None,
    )
    session.add(lead)
    session.flush()
    return lead


def import_csv(session: Session, path: Path) -> ImportResult:
    """Import a CSV with columns: company, website, and optionally contact_name, contact_email."""
    result = ImportResult()
    with path.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        missing = {"company", "website"} - set(reader.fieldnames or [])
        if missing:
            raise ValueError(f"CSV is missing required columns: {', '.join(sorted(missing))}")
        for line_no, row in enumerate(reader, start=2):
            if not row.get("company") or not row.get("website"):
                result.errors.append(f"line {line_no}: company and website are required")
                continue
            lead = add_lead(
                session,
                row["company"],
                row["website"],
                row.get("contact_name"),
                row.get("contact_email"),
            )
            if lead is None:
                result.skipped += 1
            else:
                result.added += 1
    session.commit()
    return result


async def enrich_one(
    http: httpx.AsyncClient, analyzer: Analyzer, profile: AgencyProfile, lead: LeadInput, url: str
) -> tuple[SiteSnapshot, LeadAnalysis]:
    site = await fetch_site(http, url)
    analysis = await analyzer.analyze(lead, site, profile)
    return site, analysis


def save_success(lead: Lead, site: SiteSnapshot, analysis: LeadAnalysis) -> None:
    lead.status = LeadStatus.ENRICHED
    lead.error = None
    lead.site_title = site.title
    lead.site_description = site.description
    lead.emails_found = site.emails
    lead.summary = analysis.summary
    lead.industry = analysis.industry
    lead.fit_score = analysis.fit_score
    lead.fit_reasoning = analysis.fit_reasoning
    lead.pain_points = analysis.pain_points
    lead.opportunities = analysis.automation_opportunities
    lead.email_subject = analysis.email_subject
    lead.email_body = analysis.email_body
    lead.enriched_at = datetime.now(UTC)


def save_failure(lead: Lead, error: Exception) -> None:
    lead.status = LeadStatus.FAILED
    lead.error = str(error)


async def enrich_pending(
    session_factory: sessionmaker,
    analyzer: Analyzer,
    profile: AgencyProfile,
    http: httpx.AsyncClient,
    *,
    concurrency: int = 5,
    limit: int | None = None,
    retry_failed: bool = False,
    lead_ids: list[int] | None = None,
    on_progress: Callable[[Lead], None] | None = None,
) -> EnrichResult:
    """Enrich leads concurrently.

    Network and model calls run in parallel (bounded by `concurrency`). Database writes happen
    one at a time as each lead finishes, so SQLite never sees concurrent writers.
    """
    statuses = [LeadStatus.NEW] + ([LeadStatus.FAILED] if retry_failed else [])
    with session_factory() as session:
        query = select(Lead.id, Lead.company, Lead.contact_name, Lead.website)
        if lead_ids is not None:
            query = query.where(Lead.id.in_(lead_ids))
        else:
            query = query.where(Lead.status.in_(statuses))
        rows = session.execute(query.order_by(Lead.id).limit(limit)).all()

    semaphore = asyncio.Semaphore(concurrency)

    async def worker(lead_id: int, company: str, contact: str | None, url: str):
        async with semaphore:
            try:
                return lead_id, await enrich_one(
                    http, analyzer, profile, LeadInput(company, contact), url
                )
            except (ScrapeError, AnalysisError) as exc:
                return lead_id, exc

    result = EnrichResult()
    tasks = [asyncio.create_task(worker(*row)) for row in rows]
    for finished in asyncio.as_completed(tasks):
        lead_id, outcome = await finished
        with session_factory() as session:
            lead = session.get(Lead, lead_id)
            if isinstance(outcome, Exception):
                save_failure(lead, outcome)
                result.failed += 1
            else:
                save_success(lead, *outcome)
                result.enriched += 1
            session.commit()
            if on_progress:
                on_progress(lead)
    return result


def lead_to_row(lead: Lead) -> dict:
    row = {col: getattr(lead, col) for col in EXPORT_COLUMNS}
    row["emails_found"] = "; ".join(lead.emails_found or [])
    return row


def export_csv(session: Session, path: Path, min_score: int = 0) -> int:
    query = select(Lead).order_by(Lead.fit_score.desc().nulls_last(), Lead.id)
    if min_score > 0:
        query = query.where(Lead.fit_score >= min_score)
    leads = session.scalars(query).all()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=EXPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(lead_to_row(lead) for lead in leads)
    return len(leads)
