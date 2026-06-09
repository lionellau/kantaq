"""Connection verify (MOD-14 / E22-T2): fail fast before serving.

`local` mode checks the SQLite data directory is writable. `supabase` mode checks
the URL + anon key are set and the project is reachable. Never logs the anon key.
The HTTP client is injectable so tests stay hermetic (no real network).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

import httpx

from kantaq_runtime.config import HubMode, Settings


@dataclass(frozen=True)
class VerifyResult:
    ok: bool
    message: str


def verify_connection(settings: Settings, client: httpx.Client | None = None) -> VerifyResult:
    if settings.hub_mode is HubMode.local:
        return _verify_local(settings)
    if settings.hub_mode is HubMode.supabase:
        return _verify_supabase(settings, client)
    return VerifyResult(False, f"HUB_MODE={settings.hub_mode.value} is not supported until v0.3")


def _verify_local(settings: Settings) -> VerifyResult:
    db_dir = Path(settings.local_db_path).expanduser().parent
    try:
        db_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return VerifyResult(False, f"local: cannot create data dir {db_dir}: {exc}")
    if not os.access(db_dir, os.W_OK):
        return VerifyResult(False, f"local: data dir not writable: {db_dir}")
    return VerifyResult(True, f"local: SQLite path OK ({settings.local_db_path})")


def _verify_supabase(settings: Settings, client: httpx.Client | None) -> VerifyResult:
    if not settings.supabase_url or not settings.supabase_anon_key:
        return VerifyResult(False, "supabase: SUPABASE_URL and SUPABASE_ANON_KEY are required")
    url = settings.supabase_url.rstrip("/") + "/auth/v1/health"
    owns_client = client is None
    active = client or httpx.Client(timeout=5.0)
    try:
        response = active.get(url, headers={"apikey": settings.supabase_anon_key})
    except httpx.HTTPError as exc:
        return VerifyResult(False, f"supabase: cannot reach {settings.supabase_url}: {exc}")
    finally:
        if owns_client:
            active.close()
    if response.status_code >= 500:
        return VerifyResult(False, f"supabase: backend unhealthy (HTTP {response.status_code})")
    return VerifyResult(True, f"supabase: reachable ({settings.supabase_url})")
