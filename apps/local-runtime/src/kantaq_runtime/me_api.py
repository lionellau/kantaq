"""Me API: the per-member agent snippet (E21-T2, MOD-13).

``GET /v1/me/agent-snippet`` returns the Claude Code-style MCP snippet for
**this member's own loopback gateway** — the corrected, local-first onboarding
(FR-E21-3). The gateway's bound URL comes from the ``mcp.json`` discovery file
the gateway publishes beside the local database (MOD-08); its ``pid`` field is
liveness-checked so a stale file from a hard kill does not hand out a dead URL.

SEC contract (second-review surface): this response **never carries a token**.
The runtime only stores token hashes (NFR-E06-1: plaintext appears exactly
once, in the invite/rotate response that minted it), so the snippet ships with
``token_placeholder`` and the Settings → My Agent page substitutes the
member's own session token client-side — the secret never makes a round trip.
The URL is asserted loopback before it is returned: a discovery file naming a
non-loopback host is treated as absent (fail closed), so the snippet can only
ever point an agent at the member's own machine.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Annotated, Any
from urllib.parse import urlsplit

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel
from sqlalchemy.engine import Engine

from kantaq_core.identity import VerifiedActor
from kantaq_runtime.auth import get_engine_dep, require_actor
from kantaq_runtime.config import Settings

router = APIRouter(prefix="/v1/me", tags=["me"])

EngineDep = Annotated[Engine, Depends(get_engine_dep)]
AnyActor = Annotated[VerifiedActor, Depends(require_actor)]

# The literal the web client replaces with the member's own session token.
TOKEN_PLACEHOLDER = "${KANTAQ_MEMBER_TOKEN}"

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_START_GATEWAY = (
    "the MCP gateway is not running; start it with `kantaq mcp dev` and reload this snippet"
)


class AgentSnippetOut(BaseModel):
    member_id: str
    gateway_url: str | None
    gateway_live: bool
    token_placeholder: str
    snippet: dict[str, Any] | None
    instructions: str


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def _live_gateway_url(discovery_path: Path) -> str | None:
    """The gateway URL from the discovery file, or None unless provably usable.

    Fail closed at every step: unreadable file, malformed JSON, a dead pid, or
    a non-loopback host all mean "no gateway" rather than a guessed URL.
    """
    try:
        payload = json.loads(discovery_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    url = payload.get("url")
    pid = payload.get("pid")
    if not isinstance(url, str) or not isinstance(pid, int) or not _pid_alive(pid):
        return None
    if urlsplit(url).hostname not in LOOPBACK_HOSTS:
        return None
    return url


@router.get("/agent-snippet", response_model=AgentSnippetOut)
def agent_snippet(actor: AnyActor, request: Request) -> AgentSnippetOut:
    settings: Settings = request.app.state.settings
    discovery = Path(settings.local_db_path).parent / "mcp.json"
    url = _live_gateway_url(discovery)

    if url is None:
        return AgentSnippetOut(
            member_id=actor.member_id,
            gateway_url=None,
            gateway_live=False,
            token_placeholder=TOKEN_PLACEHOLDER,
            snippet=None,
            instructions=_START_GATEWAY,
        )

    return AgentSnippetOut(
        member_id=actor.member_id,
        gateway_url=url,
        gateway_live=True,
        token_placeholder=TOKEN_PLACEHOLDER,
        snippet={
            "mcpServers": {
                "kantaq": {
                    "type": "http",
                    "url": url,
                    "headers": {"Authorization": f"Bearer {TOKEN_PLACEHOLDER}"},
                }
            }
        },
        instructions=(
            "save as .mcp.json in your project (Claude Code), replacing "
            f"{TOKEN_PLACEHOLDER} with your member token"
        ),
    )
