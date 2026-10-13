from pathlib import Path

import pytest

from leadsmith.config import load_profile
from leadsmith.db import make_engine, make_session_factory
from leadsmith.demo import make_demo_transport
from leadsmith.scraper import make_http_client

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def profile():
    return load_profile(ROOT / "agency_profile.yaml")


@pytest.fixture
def db_url(tmp_path):
    return f"sqlite:///{tmp_path / 'test.db'}"


@pytest.fixture
def session_factory(db_url):
    return make_session_factory(make_engine(db_url))


@pytest.fixture
async def http():
    async with make_http_client(transport=make_demo_transport()) as client:
        yield client


@pytest.fixture
def leads_csv():
    return ROOT / "examples" / "leads.csv"
