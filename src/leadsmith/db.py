from datetime import UTC, datetime
from enum import StrEnum

from sqlalchemy import JSON, DateTime, String, Text, create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


class Base(DeclarativeBase):
    pass


class LeadStatus(StrEnum):
    NEW = "new"
    ENRICHED = "enriched"
    FAILED = "failed"


def utcnow() -> datetime:
    return datetime.now(UTC)


class Lead(Base):
    __tablename__ = "leads"

    id: Mapped[int] = mapped_column(primary_key=True)
    company: Mapped[str] = mapped_column(String(200))
    website: Mapped[str] = mapped_column(String(500), unique=True, index=True)
    contact_name: Mapped[str | None] = mapped_column(String(200))
    contact_email: Mapped[str | None] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(20), default=LeadStatus.NEW, index=True)
    error: Mapped[str | None] = mapped_column(Text)

    # Scraped from the company's website
    site_title: Mapped[str | None] = mapped_column(String(500))
    site_description: Mapped[str | None] = mapped_column(Text)
    emails_found: Mapped[list[str]] = mapped_column(JSON, default=list)

    # Produced by the analyzer
    summary: Mapped[str | None] = mapped_column(Text)
    industry: Mapped[str | None] = mapped_column(String(200))
    fit_score: Mapped[int | None] = mapped_column(index=True)
    fit_reasoning: Mapped[str | None] = mapped_column(Text)
    pain_points: Mapped[list[str]] = mapped_column(JSON, default=list)
    opportunities: Mapped[list[str]] = mapped_column(JSON, default=list)
    email_subject: Mapped[str | None] = mapped_column(String(300))
    email_body: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    enriched_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


def make_engine(database_url: str) -> Engine:
    connect_args = {}
    if database_url.startswith("sqlite"):
        # FastAPI serves requests from a thread pool, so the connection must be shareable.
        connect_args["check_same_thread"] = False
    engine = create_engine(database_url, connect_args=connect_args)

    if database_url.startswith("sqlite"):

        @event.listens_for(engine, "connect")
        def _enable_wal(dbapi_conn, _record):
            # WAL lets the dashboard read while the enrichment job writes.
            dbapi_conn.execute("PRAGMA journal_mode=WAL")

    Base.metadata.create_all(engine)
    return engine


def make_session_factory(engine: Engine) -> sessionmaker:
    return sessionmaker(engine, expire_on_commit=False)
