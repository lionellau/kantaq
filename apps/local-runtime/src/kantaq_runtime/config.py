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


class ProposalStalePolicy(StrEnum):
    """How a stale agent proposal is handled on sync (MOD-26 §B3 / E05-T3).

    When a human approves an agent proposal whose ticket write turns out to be
    based on a revision the team has moved past, this decides whether to bounce
    it back to ``rebase_required`` for re-decision. Either way the agent's stale
    value never silently wins (the §8.5 propose-first rule).
    """

    # Smooth default: bounce only on a genuine field clash; a proposal that
    # touched a different field than the intervening edit auto-merges, so the
    # human is never nagged for a needless re-approval.
    auto_rebase = "auto_rebase"
    # Conservative: re-confirm every proposal that raced any change since it was
    # made, conflicting or not.
    strict_rebase = "strict_rebase"


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
    # Self-hosted backend (HUB_MODE=postgres, MOD-28 / E25). `hub_url` is the
    # sync-server base URL (e.g. https://kantaq.team.example or http://host:8889);
    # `hub_token` is the member's bearer token the server authenticates the
    # caller with (the same token/grant auth as Supabase mode — OIDC deferred,
    # DEBT-14). Both come from `.env` (see docker/self-hosted-backend/.env.self-hosted.example).
    hub_url: str | None = None
    hub_token: str | None = None
    # The signing cutover switch (E04-T4 / FR-E04-6). Off until a workspace
    # deliberately cuts over to signed sync; the cutover is a recorded,
    # one-way decision (dev-planning D-15) because pre-cutover events stay
    # unsigned-but-immutable history. When on, every new event the runtime
    # writes is Ed25519-signed under the member's capability grant and an
    # unsigned write fails closed locally; the backend then rejects unsigned
    # or grant-less events past the cutover revision (E24-T5).
    sign_events: bool = False
    # The cutover revision (D-15): committed events at or below it are
    # pre-cutover, unsigned-immutable history that verified ingestion passes
    # through; everything above it must verify. A fresh workspace cuts over at
    # 0 (sign from the start); an existing one at its current backend head.
    sign_cutover_rev: int = 0
    # How a stale agent proposal is handled on sync (MOD-26 §B3 / E05-T3). A
    # workspace setting: ``auto_rebase`` (default) only re-decides a proposal
    # that genuinely conflicts; ``strict_rebase`` re-confirms any that raced a
    # change. Set via env / .env (AGENT_PROPOSAL_STALE_POLICY); surfaced
    # read-only in Settings → Sync so the active policy is discoverable.
    agent_proposal_stale_policy: ProposalStalePolicy = ProposalStalePolicy.auto_rebase


def get_settings() -> Settings:
    """Load settings from the environment / `.env`."""
    return Settings()
