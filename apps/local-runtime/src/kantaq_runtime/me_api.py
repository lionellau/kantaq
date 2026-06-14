"""Me API: the signed-in member and their loopback agent snippet (MOD-13/MOD-12).

``GET /v1/me`` (E20-T2, MOD-12 Settings → Identity) returns who the bearer
token belongs to — member id, email, role, the token's scopes, and the
workspace it lives in. Read-only and self-scoped: the response is always the
caller's own identity, never another member's, so no permission beyond a valid
token is needed. It carries no secret (the token came in, nothing key-shaped
goes out).

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

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.engine import Engine
from sqlmodel import Session

from kantaq_core.identity import VerifiedActor
from kantaq_db.models import Member, Workspace
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


class AgentClientSnippet(BaseModel):
    """One Tier-1 client's copy-paste MCP config (E11-T2, MOD-24 Tier-1).

    The same loopback URL + placeholder bearer, shaped for each client's config
    file. Claude Code reads ``.mcp.json`` (``type: http`` + ``url``); Cursor
    reads ``.cursor/mcp.json`` (``url`` for a remote/streamable-HTTP server).
    Like the parent response, this **never** carries a token — only the
    placeholder the web client substitutes locally.
    """

    client: str
    label: str
    config: dict[str, Any]
    save_as: str
    instructions: str


class AgentSnippetOut(BaseModel):
    member_id: str
    gateway_url: str | None
    gateway_live: bool
    token_placeholder: str
    # Back-compat: the Claude Code config (== the ``claude_code`` entry of
    # ``clients``). Kept so existing consumers keep working; new clients read
    # ``clients`` to offer every Tier-1 target.
    snippet: dict[str, Any] | None
    clients: list[AgentClientSnippet]
    instructions: str


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


# The discovery file is a handful of short fields; anything bigger is not ours
# (SEC second review: an unbounded read_text is a same-user DoS vector).
_DISCOVERY_MAX_BYTES = 64 * 1024


def _live_gateway_url(discovery_path: Path) -> str | None:
    """The gateway URL from the discovery file, or None unless provably usable.

    Fail closed at every step: unreadable, symlinked-away, oversized, or
    malformed files, a dead pid, or a non-loopback host all mean "no gateway"
    rather than a guessed URL.
    """
    try:
        # The gateway writes a regular file in the data directory; a symlink
        # pointing elsewhere is someone else's payload (SEC second review).
        resolved = discovery_path.resolve(strict=True)
        if resolved.parent != discovery_path.parent.resolve():
            return None
        with resolved.open("rb") as handle:
            raw = handle.read(_DISCOVERY_MAX_BYTES + 1)
        if len(raw) > _DISCOVERY_MAX_BYTES:
            return None
        payload = json.loads(raw)
    except (OSError, ValueError):
        return None
    url = payload.get("url")
    pid = payload.get("pid")
    if not isinstance(url, str) or not isinstance(pid, int) or not _pid_alive(pid):
        return None
    if urlsplit(url).hostname not in LOOPBACK_HOSTS:
        return None
    return url


def _client_snippets(url: str) -> list[AgentClientSnippet]:
    """The Tier-1 clients' MCP configs for a live loopback gateway (E11-T2).

    One server entry per client, differing only where the client's config schema
    differs: Claude Code's ``.mcp.json`` names the transport (``type: http``);
    Cursor's ``.cursor/mcp.json`` takes a bare ``url`` for a remote server. Both
    carry the placeholder bearer, never a real token.
    """
    bearer = {"Authorization": f"Bearer {TOKEN_PLACEHOLDER}"}
    return [
        AgentClientSnippet(
            client="claude_code",
            label="Claude Code",
            config={"mcpServers": {"kantaq": {"type": "http", "url": url, "headers": bearer}}},
            save_as=".mcp.json",
            instructions=(
                "save as .mcp.json in your project (Claude Code reads it on start), "
                f"replacing {TOKEN_PLACEHOLDER} with your member token"
            ),
        ),
        AgentClientSnippet(
            client="cursor",
            label="Cursor",
            config={"mcpServers": {"kantaq": {"url": url, "headers": bearer}}},
            save_as=".cursor/mcp.json",
            instructions=(
                "save as .cursor/mcp.json in your project (or ~/.cursor/mcp.json for every "
                f"project), then replace {TOKEN_PLACEHOLDER} with your member token"
            ),
        ),
    ]


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
            clients=[],
            instructions=_START_GATEWAY,
        )

    clients = _client_snippets(url)
    claude_code = clients[0]
    return AgentSnippetOut(
        member_id=actor.member_id,
        gateway_url=url,
        gateway_live=True,
        token_placeholder=TOKEN_PLACEHOLDER,
        snippet=claude_code.config,  # back-compat: the Claude Code config
        clients=clients,
        instructions=claude_code.instructions,
    )


class MeOut(BaseModel):
    member_id: str
    email: str
    role: str
    scopes: list[str]
    workspace_id: str
    workspace_name: str


@router.get("", response_model=MeOut)
def me(actor: AnyActor, engine: EngineDep) -> MeOut:
    """The signed-in member's own identity (E20-T2, Settings → Identity).

    Self-scoped by construction: the member id comes from the verified token,
    so there is no way to ask for someone else. ``scopes`` are the token's (a
    human token carries none — the role decides; an agent token carries its
    propose-first scopes).
    """
    with Session(engine) as session:
        member = session.get(Member, actor.member_id)
        if member is None:
            # The token verified but its member row is gone (revoked + pruned):
            # treat as unauthenticated rather than inventing an identity.
            raise HTTPException(status_code=404, detail="member not found")
        workspace = session.get(Workspace, member.workspace_id)
        return MeOut(
            member_id=member.id,
            email=member.email,
            role=member.role,
            scopes=list(actor.scopes),
            workspace_id=member.workspace_id,
            workspace_name=workspace.name if workspace is not None else "",
        )
