import os
from functools import lru_cache
from pathlib import Path
from typing import Literal

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, read from LEADSMITH_* environment variables or a .env file."""

    model_config = SettingsConfigDict(env_prefix="LEADSMITH_", env_file=".env", extra="ignore")

    database_url: str = "sqlite:///leadsmith.db"
    api_key: str = "change-me"
    llm_mode: Literal["claude", "fake"] = "claude"
    claude_model: str = "claude-opus-5-5"
    concurrency: int = 5
    request_timeout: float = 15.0
    profile_path: Path = Path("agency_profile.yaml")


class AgencyProfile(BaseModel):
    """Who we are and who we sell to. Fed into every analysis prompt."""

    agency_name: str
    sender_name: str
    services: list[str]
    ideal_customer: str
    good_fit_signals: list[str]
    bad_fit_signals: list[str] = []
    tone: str = "friendly, concise, plain American English, no hype"
    call_to_action: str = "Open to a quick 15-minute call next week?"


def load_profile(path: Path) -> AgencyProfile:
    with path.open() as f:
        return AgencyProfile.model_validate(yaml.safe_load(f))


def has_claude_credentials() -> bool:
    """True if the Anthropic SDK has something to authenticate with."""
    return bool(
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("ANTHROPIC_AUTH_TOKEN")
        or (Path.home() / ".config" / "anthropic").is_dir()  # an `ant auth login` profile
    )


@lru_cache
def get_settings() -> Settings:
    # Load .env into the process environment too, so the Anthropic SDK sees
    # ANTHROPIC_API_KEY (pydantic-settings only reads the prefixed variables).
    load_dotenv()
    return Settings()
