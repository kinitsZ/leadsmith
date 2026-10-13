import csv
import io

import pytest
from fastapi.testclient import TestClient

from leadsmith.analyzer import FakeAnalyzer
from leadsmith.api import create_app
from leadsmith.config import Settings
from leadsmith.demo import make_demo_transport
from leadsmith.scraper import make_http_client

KEY = {"X-API-Key": "test-key"}


@pytest.fixture
def client(db_url, profile):
    settings = Settings(database_url=db_url, api_key="test-key", llm_mode="fake")
    app = create_app(
        settings,
        analyzer=FakeAnalyzer(),
        profile=profile,
        http_client_factory=lambda: make_http_client(transport=make_demo_transport()),
    )
    with TestClient(app) as client:
        yield client


def test_requires_api_key(client):
    assert client.get("/api/leads").status_code == 401
    assert client.get("/api/leads", headers={"X-API-Key": "wrong"}).status_code == 401
    assert client.get("/health").status_code == 200


def test_create_and_enrich_lead(client):
    body = {
        "company": "BrightSmile",
        "website": "https://brightsmiledental.test",
        "contact_name": "Dana",
    }
    created = client.post("/api/leads", json=body, headers=KEY)
    assert created.status_code == 201
    lead_id = created.json()["id"]

    duplicate = client.post("/api/leads", json=body, headers=KEY)
    assert duplicate.status_code == 409

    enriched = client.post(f"/api/leads/{lead_id}/enrich", headers=KEY).json()
    assert enriched["status"] == "enriched"
    assert enriched["fit_score"] >= 75
    assert enriched["email_body"].startswith("Hi Dana,")


def test_rejects_invalid_lead(client):
    response = client.post("/api/leads", json={"company": "X", "website": "not a url"}, headers=KEY)
    assert response.status_code == 422


def test_import_list_filter_and_export(client, leads_csv):
    with leads_csv.open("rb") as f:
        summary = client.post("/api/leads/import", files={"file": f}, headers=KEY).json()
    assert summary == {"added": 5, "skipped": 0, "errors": []}

    for lead in client.get("/api/leads", headers=KEY).json():
        client.post(f"/api/leads/{lead['id']}/enrich", headers=KEY)

    hot = client.get("/api/leads", params={"min_score": 75}, headers=KEY).json()
    assert [lead["company"] for lead in hot] == ["BrightSmile Dental Group"]

    failed = client.get("/api/leads", params={"status": "failed"}, headers=KEY).json()
    assert [lead["company"] for lead in failed] == ["Old Mill Plumbing"]

    rows = list(csv.DictReader(io.StringIO(client.get("/api/leads/export.csv", headers=KEY).text)))
    assert len(rows) == 5
    assert rows[0]["company"] == "BrightSmile Dental Group"  # sorted by score


def test_unknown_lead_returns_404(client):
    assert client.get("/api/leads/999", headers=KEY).status_code == 404
    assert client.post("/api/leads/999/enrich", headers=KEY).status_code == 404


def test_dashboard_renders(client, leads_csv):
    with leads_csv.open("rb") as f:
        client.post("/api/leads/import", files={"file": f}, headers=KEY)
    page = client.get("/")
    assert page.status_code == 200
    assert "Greenleaf Bakery" in page.text
