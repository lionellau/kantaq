"""Runtime configuration (MOD-14 / E22-T2).

One config switch (`HUB_MODE`) selects where committed state syncs. Values are
read from the environment and an optional `.env` file via pydantic-settings.
Secrets (the Supabase anon key) are held here but never logged.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic_settings import BaseSettings, SettingsConfigDict


class HubMode(StrEnum):
    """Where committed state syncs. `postgres` (self-host) lands in v0.3."""

    local = "local"
    supabase = "supabase"
    postgres = "postgres"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    hub_mode: HubMode = HubMode.local
    host: str = "127.0.0.1"
    port: int = 3939
    local_db_path: str = "./data/local.sqlite"
    local_mcp_host: str = "127.0.0.1"
    local_mcp_port: str = "auto"
    supabase_url: str | None = None
    supabase_anon_key: str | None = None
    # The signing cutover switch (E04-T4 / FR-E04-6). Off until a workspace
    # deliberately cuts over to signed sync; the cutover is a recorded,
    # one-way decision (dev-planning D-15) because pre-cutover events stay
    # unsigned-but-immutable history. When on, every new event the runtime
    # writes is Ed25519-signed under the member's capability grant and an
    # unsigned write fails closed locally; the backend then rejects unsigned
    # or grant-less events past the cutover revision (E24-T5).
    sign_events: bool = False


def get_settings() -> Settings:
    """Load settings from the environment / `.env`."""
    return Settings()
