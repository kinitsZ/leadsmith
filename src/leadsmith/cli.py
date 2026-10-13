"""Command-line interface: `leadsmith --help`."""

import asyncio
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table
from sqlalchemy import select

from .analyzer import FakeAnalyzer
from .api import build_analyzer
from .config import get_settings, load_profile
from .db import Lead, make_engine, make_session_factory
from .demo import EXAMPLES_DIR, make_demo_transport
from .pipeline import enrich_pending, export_csv, import_csv
from .scraper import make_http_client

app = typer.Typer(help="AI lead research and outreach pipeline.", no_args_is_help=True)
console = Console()


def _session_factory():
    return make_session_factory(make_engine(get_settings().database_url))


def _score_style(score: int | None) -> str:
    if score is None:
        return "dim"
    return "green" if score >= 75 else "yellow" if score >= 50 else "red"


@app.command("import")
def import_leads(csv_path: Annotated[Path, typer.Argument(exists=True, dir_okay=False)]):
    """Import leads from a CSV (columns: company, website, contact_name, contact_email)."""
    with _session_factory()() as session:
        result = import_csv(session, csv_path)
    console.print(f"[green]Added {result.added}[/] leads, skipped {result.skipped} duplicates.")
    for error in result.errors:
        console.print(f"[yellow]  {error}[/]")


@app.command()
def enrich(
    limit: Annotated[int | None, typer.Option(help="Max leads to process.")] = None,
    concurrency: Annotated[int | None, typer.Option(help="Parallel requests.")] = None,
    retry_failed: Annotated[bool, typer.Option(help="Also retry leads that failed.")] = False,
    fake: Annotated[bool, typer.Option(help="Use the offline analyzer instead of Claude.")] = False,
    offline_sites: Annotated[
        bool, typer.Option(help="Serve websites from examples/sites instead of the internet.")
    ] = False,
):
    """Scrape each pending lead's website, score it, and draft an outreach email."""
    settings = get_settings()
    analyzer = FakeAnalyzer() if fake else build_analyzer(settings)
    profile = load_profile(settings.profile_path)
    transport = make_demo_transport() if offline_sites else None

    def show(lead: Lead) -> None:
        if lead.status == "failed":
            console.print(f"  [red]✗[/] {lead.company}: {lead.error}")
        else:
            style = _score_style(lead.fit_score)
            console.print(f"  [green]✓[/] {lead.company} [{style}]{lead.fit_score}[/]")

    async def run():
        async with make_http_client(settings.request_timeout, transport=transport) as http:
            return await enrich_pending(
                _session_factory(),
                analyzer,
                profile,
                http,
                concurrency=concurrency or settings.concurrency,
                limit=limit,
                retry_failed=retry_failed,
                on_progress=show,
            )

    result = asyncio.run(run())
    console.print(f"\nEnriched {result.enriched}, failed {result.failed}.")


@app.command("list")
def list_leads(
    min_score: Annotated[int, typer.Option(help="Only show leads scoring at least this.")] = 0,
):
    """Show leads ranked by fit score."""
    with _session_factory()() as session:
        query = select(Lead).order_by(Lead.fit_score.desc().nulls_last(), Lead.id)
        if min_score:
            query = query.where(Lead.fit_score >= min_score)
        leads = session.scalars(query).all()

    table = Table("Score", "Company", "Industry", "Status", "Subject line")
    for lead in leads:
        score = "-" if lead.fit_score is None else str(lead.fit_score)
        table.add_row(
            f"[{_score_style(lead.fit_score)}]{score}[/]",
            lead.company,
            lead.industry or "",
            lead.status,
            lead.email_subject or (lead.error or ""),
        )
    console.print(table)


@app.command()
def show(lead_id: int):
    """Print the full research and email draft for one lead."""
    with _session_factory()() as session:
        lead = session.get(Lead, lead_id)
    if lead is None:
        raise typer.BadParameter(f"No lead with id {lead_id}")
    console.rule(f"{lead.company} ({lead.fit_score if lead.fit_score is not None else '-'})")
    console.print(f"[dim]{lead.website}[/]  {lead.industry or ''}\n")
    console.print(lead.summary or lead.error or "Not enriched yet.")
    if lead.fit_reasoning:
        console.print(f"\n[bold]Why:[/] {lead.fit_reasoning}")
    for item in lead.opportunities:
        console.print(f"  • {item}")
    if lead.email_body:
        console.print(f"\n[bold]Subject:[/] {lead.email_subject}\n\n{lead.email_body}")


@app.command()
def export(
    path: Annotated[Path, typer.Argument()] = Path("exports/leads.csv"),
    min_score: Annotated[int, typer.Option()] = 0,
):
    """Export leads to CSV for a CRM or mail-merge tool."""
    with _session_factory()() as session:
        count = export_csv(session, path, min_score)
    console.print(f"Wrote {count} leads to {path}")


@app.command()
def demo(
    fake: Annotated[bool, typer.Option(help="Skip Claude and use the offline analyzer.")] = False,
):
    """Import the example leads and enrich them against the bundled example websites."""
    import_leads(EXAMPLES_DIR / "leads.csv")
    enrich(limit=None, concurrency=None, retry_failed=True, fake=fake, offline_sites=True)
    list_leads(min_score=0)


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False):
    """Run the REST API and dashboard."""
    import uvicorn

    uvicorn.run("leadsmith.api:create_app", factory=True, host=host, port=port, reload=reload)


if __name__ == "__main__":
    app()
