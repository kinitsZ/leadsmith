"""REST API and a small dashboard on top of the lead database."""

import csv
import io
import logging
import secrets
import tempfile
from collections.abc import Callable
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Query, Request, Security, UploadFile, status
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel, ConfigDict, EmailStr, HttpUrl
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from .analyzer import Analyzer, ClaudeAnalyzer, FakeAnalyzer
from .config import AgencyProfile, Settings, get_settings, has_claude_credentials, load_profile
from .db import Lead, LeadStatus, make_engine, make_session_factory
from .pipeline import EXPORT_COLUMNS, add_lead, enrich_pending, import_csv, lead_to_row
from .scraper import make_http_client

log = logging.getLogger("leadsmith")
TEMPLATES = Jinja2Templates(directory=Path(__file__).parent / "templates")


class LeadCreate(BaseModel):
    company: str
    website: HttpUrl
    contact_name: str | None = None
    contact_email: EmailStr | None = None


class LeadOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    company: str
    website: str
    contact_name: str | None
    contact_email: str | None
    status: str
    error: str | None
    fit_score: int | None
    industry: str | None
    summary: str | None
    fit_reasoning: str | None
    pain_points: list[str]
    opportunities: list[str]
    emails_found: list[str]
    email_subject: str | None
    email_body: str | None
    created_at: datetime
    enriched_at: datetime | None


class ImportSummary(BaseModel):
    added: int
    skipped: int
    errors: list[str]


def build_analyzer(settings: Settings) -> Analyzer:
    if settings.llm_mode == "fake":
        return FakeAnalyzer()
    if not has_claude_credentials():
        log.warning("No ANTHROPIC_API_KEY found, so using the offline analyzer.")
        return FakeAnalyzer()
    return ClaudeAnalyzer(model=settings.claude_model)


def create_app(
    settings: Settings | None = None,
    analyzer: Analyzer | None = None,
    profile: AgencyProfile | None = None,
    http_client_factory: Callable[[], httpx.AsyncClient] | None = None,
) -> FastAPI:
    settings = settings or get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        engine = make_engine(settings.database_url)
        app.state.session_factory = make_session_factory(engine)
        app.state.analyzer = analyzer or build_analyzer(settings)
        app.state.profile = profile or load_profile(settings.profile_path)
        app.state.http_client_factory = http_client_factory or (
            lambda: make_http_client(settings.request_timeout)
        )
        yield
        engine.dispose()

    app = FastAPI(
        title="LeadSmith",
        description="AI lead research and outreach pipeline.",
        version="0.1.0",
        lifespan=lifespan,
    )

    api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)

    def require_api_key(key: Annotated[str | None, Security(api_key_header)]) -> None:
        # compare_digest avoids leaking the key through response-time differences.
        if not key or not secrets.compare_digest(key, settings.api_key):
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "Missing or invalid X-API-Key")

    def get_session(request: Request):
        with request.app.state.session_factory() as session:
            yield session

    SessionDep = Annotated[Session, Depends(get_session)]
    protected = [Depends(require_api_key)]

    @app.get("/health")
    def health() -> dict:
        return {"status": "ok"}

    @app.get("/api/leads", response_model=list[LeadOut], dependencies=protected)
    def list_leads(
        session: SessionDep,
        min_score: Annotated[int, Query(ge=0, le=100)] = 0,
        lead_status: Annotated[LeadStatus | None, Query(alias="status")] = None,
        limit: Annotated[int, Query(ge=1, le=500)] = 100,
        offset: Annotated[int, Query(ge=0)] = 0,
    ):
        query = select(Lead).order_by(Lead.fit_score.desc().nulls_last(), Lead.id)
        if min_score:
            query = query.where(Lead.fit_score >= min_score)
        if lead_status:
            query = query.where(Lead.status == lead_status)
        return session.scalars(query.limit(limit).offset(offset)).all()

    @app.post(
        "/api/leads",
        response_model=LeadOut,
        status_code=status.HTTP_201_CREATED,
        dependencies=protected,
    )
    def create_lead(body: LeadCreate, session: SessionDep):
        lead = add_lead(
            session, body.company, str(body.website), body.contact_name, body.contact_email
        )
        if lead is None:
            raise HTTPException(status.HTTP_409_CONFLICT, "A lead with this website already exists")
        session.commit()
        return lead

    @app.get("/api/leads/export.csv", dependencies=protected)
    def export_leads(session: SessionDep, min_score: Annotated[int, Query(ge=0, le=100)] = 0):
        query = select(Lead).order_by(Lead.fit_score.desc().nulls_last(), Lead.id)
        if min_score:
            query = query.where(Lead.fit_score >= min_score)
        buffer = io.StringIO()
        writer = csv.DictWriter(buffer, fieldnames=EXPORT_COLUMNS)
        writer.writeheader()
        writer.writerows(lead_to_row(lead) for lead in session.scalars(query))
        buffer.seek(0)
        return StreamingResponse(
            buffer,
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="leads.csv"'},
        )

    @app.get("/api/leads/{lead_id}", response_model=LeadOut, dependencies=protected)
    def get_lead(lead_id: int, session: SessionDep):
        lead = session.get(Lead, lead_id)
        if lead is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
        return lead

    @app.post("/api/leads/import", response_model=ImportSummary, dependencies=protected)
    async def upload_csv(file: UploadFile, session: SessionDep):
        with tempfile.NamedTemporaryFile(suffix=".csv") as tmp:
            tmp.write(await file.read())
            tmp.flush()
            try:
                result = import_csv(session, Path(tmp.name))
            except (ValueError, UnicodeDecodeError) as exc:
                raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(exc)) from exc
        return ImportSummary(added=result.added, skipped=result.skipped, errors=result.errors)

    @app.post("/api/leads/{lead_id}/enrich", response_model=LeadOut, dependencies=protected)
    async def enrich_lead(lead_id: int, request: Request):
        state = request.app.state
        with state.session_factory() as session:
            if session.get(Lead, lead_id) is None:
                raise HTTPException(status.HTTP_404_NOT_FOUND, "Lead not found")
        async with state.http_client_factory() as http:
            await enrich_pending(
                state.session_factory, state.analyzer, state.profile, http, lead_ids=[lead_id]
            )
        with state.session_factory() as session:
            return session.get(Lead, lead_id)

    @app.get("/", response_class=HTMLResponse, include_in_schema=False)
    def dashboard(request: Request, session: SessionDep, min_score: int = 0):
        # Read-only view meant for local use. Put it behind auth before exposing it publicly.
        query = select(Lead).order_by(Lead.fit_score.desc().nulls_last(), Lead.id)
        if min_score:
            query = query.where(Lead.fit_score >= min_score)
        counts = dict(
            session.execute(select(Lead.status, func.count()).group_by(Lead.status)).all()
        )
        return TEMPLATES.TemplateResponse(
            request,
            "dashboard.html",
            {"leads": session.scalars(query).all(), "counts": counts, "min_score": min_score},
        )

    return app
