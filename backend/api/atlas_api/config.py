"""Runtime configuration (env vars / .env). See backend/.env.example."""
from __future__ import annotations

import socket
from functools import lru_cache
from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict

_REPO_ROOT = Path(__file__).resolve().parents[3]


class Settings(BaseSettings):
    # backend/.env, then ./.env (later wins); real env vars beat both.
    model_config = SettingsConfigDict(env_file=(_REPO_ROOT / "backend" / ".env", ".env"), extra="ignore")

    data_mode: Literal["fixtures", "db"] = "fixtures"
    # In fixtures mode, endpoints listed here are served from Postgres anyway
    # (switch endpoint by endpoint), e.g. "search,node,neighborhood".
    db_endpoints: str = ""
    fixtures_dir: Path = _REPO_ROOT / "contract" / "fixtures"

    database_url: str | None = None
    db_pool_size: int = 5
    redis_url: str | None = None

    openai_api_key: str | None = None
    openai_model: str = "gpt-4o-mini"
    brightdata_api_key: str | None = None
    brightdata_serp_zone: str = "serp_api1"

    cors_origins: str = ""
    instance_id: str = socket.gethostname()
    log_level: str = "INFO"

    cache_ttl_seconds: int = 300
    ai_rate_limit_per_minute: int = 10
    shutdown_grace_seconds: float = 0.0
    job_ttl_seconds: int = 3600

    def uses_db(self, endpoint: str) -> bool:
        if self.data_mode == "db":
            return True
        return endpoint in {e.strip() for e in self.db_endpoints.split(",") if e.strip()}

    @property
    def needs_db(self) -> bool:
        return self.data_mode == "db" or bool(self.db_endpoints.strip())


@lru_cache
def get_settings() -> Settings:
    return Settings()
