"""Me API: the signed-in member and their loopback agent snippet (MOD-13/MOD-12).

``GET /v1/me`` (E20-T2, MOD-12 Settings → Identity) returns who the bearer
token belongs to — member id, email, role, the token's scopes, and the
workspace it lives in. Read-only and self-scoped: the response is always the
caller's own identity, never another member's, so no permission beyond a valid
token is needed. It carries no secret (the token came in, nothing key-shaped
goes out).

``GET /v1/me/agent-snippet`` returns the MCP connection snippets for **this
member's own loopback gateway** — one per compatible client (Claude Code,
Cursor, Codex; E11/MOD-24), with the bare ``snippet`` field kept as the Claude
Code config for back-compat — the corrected, local-first onboarding (FR-E21-3).
The gateway's bound URL comes from the ``mcp.json`` discovery file the gateway
publishes beside the local database (MOD-08); its ``pid`` field is
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
from kantaq_mcp.stdio import TOKEN_ENV as MCP_STDIO_TOKEN_ENV
from kantaq_runtime.auth import get_engine_dep, require_actor
from kantaq_runtime.config import Settings

router = APIRouter(prefix="/v1/me", tags=["me"])

EngineDep = Annotated[Engine, Depends(get_engine_dep)]
AnyActor = Annotated[VerifiedActor, Depends(require_actor)]

# The literal the web client replaces with the member's own session token.
TOKEN_PLACEHOLDER = "${KANTAQ_MEMBER_TOKEN}"

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

_START_GATEWAY = (
    "the HTTP MCP gateway is not running; the stdio configs below work without it "
    "(the client launches `kantaq mcp stdio` itself). For the HTTP option, start the "
    "gateway with `kantaq mcp dev` and reload this snippet"
)


# Codex reads its MCP bearer from an env var (never the config file) — the
# token stays out of `~/.codex/config.toml` entirely.
CODEX_TOKEN_ENV = "KANTAQ_AGENT_TOKEN"

FORMAT_MCP_JSON = "mcp_json"
FORMAT_TOML = "toml"


class AgentClientSnippet(BaseModel):
    """One compatible client's copy-paste MCP config (E11, MOD-24).

    The same loopback URL, shaped for each client's config file and auth style:

    - **Claude Code** — ``.mcp.json`` (``type: http`` + ``url`` + an inline
      ``Authorization`` bearer).
    - **Cursor** — ``.cursor/mcp.json`` (bare ``url`` for a remote server + the
      inline bearer).
    - **Codex** — ``~/.codex/config.toml`` (`[mcp_servers.kantaq]` with ``url``
      + ``bearer_token_env_var``); the token rides an **env var**, never the
      file (``setup`` carries the export).

    ``text`` is the exact string to paste, rendered for ``format`` (``mcp_json``
    or ``toml``). Like the parent response, nothing here carries a real token —
    only the placeholder the web client substitutes locally, wherever it appears
    (``text`` for the header clients, ``setup`` for Codex).
    """

    client: str
    label: str
    config: dict[str, Any]
    format: str
    text: str
    save_as: str
    setup: str | None
    # "http" (the live loopback gateway) or "stdio" (the client spawns
    # `kantaq mcp stdio` itself). Both run the exact same eight checks + audit
    # (E09-T4/T5); the client picks whichever transport it speaks.
    transport: str = "http"
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


def _mcp_json(config: dict[str, Any]) -> str:
    """Render an mcp.json config as the exact string a client config file holds."""
    return json.dumps(config, indent=2)


def _codex_toml(url: str) -> str:
    """Render the Codex `[mcp_servers.kantaq]` TOML table (no token — env-var auth).

    The loopback URL has no TOML-special characters; the bearer rides
    ``CODEX_TOKEN_ENV`` (the ``setup`` export), never the file.
    """
    return f'[mcp_servers.kantaq]\nurl = "{url}"\nbearer_token_env_var = "{CODEX_TOKEN_ENV}"'


def _http_snippets(url: str) -> list[AgentClientSnippet]:
    """Each client's HTTP MCP config for a live loopback gateway (E11).

    One entry per client, differing only where the client's config schema and
    auth style differ: Claude Code's ``.mcp.json`` names the transport
    (``type: http``) with an inline bearer; Cursor's ``.cursor/mcp.json`` takes a
    bare ``url`` + inline bearer; Codex's ``~/.codex/config.toml`` takes a
    ``[mcp_servers.kantaq]`` table and reads the bearer from an env var (the
    token never touches the file). None carries a real token — only the
    placeholder.
    """
    bearer = {"Authorization": f"Bearer {TOKEN_PLACEHOLDER}"}
    claude_config: dict[str, Any] = {
        "mcpServers": {"kantaq": {"type": "http", "url": url, "headers": bearer}}
    }
    cursor_config: dict[str, Any] = {"mcpServers": {"kantaq": {"url": url, "headers": bearer}}}
    codex_config: dict[str, Any] = {
        "mcp_servers": {"kantaq": {"url": url, "bearer_token_env_var": CODEX_TOKEN_ENV}}
    }
    return [
        AgentClientSnippet(
            client="claude_code",
            label="Claude Code (HTTP)",
            config=claude_config,
            format=FORMAT_MCP_JSON,
            text=_mcp_json(claude_config),
            save_as=".mcp.json",
            setup=None,
            transport="http",
            instructions=(
                "save as .mcp.json in your project (Claude Code reads it on start), "
                f"replacing {TOKEN_PLACEHOLDER} with your member token"
            ),
        ),
        AgentClientSnippet(
            client="cursor",
            label="Cursor (HTTP)",
            config=cursor_config,
            format=FORMAT_MCP_JSON,
            text=_mcp_json(cursor_config),
            save_as=".cursor/mcp.json",
            setup=None,
            transport="http",
            instructions=(
                "save as .cursor/mcp.json in your project (or ~/.cursor/mcp.json for every "
                f"project), then replace {TOKEN_PLACEHOLDER} with your member token"
            ),
        ),
        AgentClientSnippet(
            client="codex",
            label="Codex (HTTP)",
            config=codex_config,
            format=FORMAT_TOML,
            text=_codex_toml(url),
            save_as="~/.codex/config.toml",
            setup=f"export {CODEX_TOKEN_ENV}={TOKEN_PLACEHOLDER}",
            transport="http",
            instructions=(
                "add this table to ~/.codex/config.toml, then export your member token as "
                f"{CODEX_TOKEN_ENV} (Codex reads the bearer from the env var, never the file)"
            ),
        ),
    ]


def _stdio_command_config() -> dict[str, Any]:
    """The Claude Code / Cursor stdio server block — spawn `kantaq mcp stdio`.

    The bearer rides the ``KANTAQ_MCP_TOKEN`` env the child reads at startup, so
    it never lands in the config file's server URL. No gateway URL: the client
    launches the gateway itself.
    """
    return {
        "mcpServers": {
            "kantaq": {
                "command": "kantaq",
                "args": ["mcp", "stdio"],
                "env": {MCP_STDIO_TOKEN_ENV: TOKEN_PLACEHOLDER},
            }
        }
    }


def _codex_stdio_toml() -> str:
    """The Codex stdio server table — command + args + the bearer in an env block."""
    return (
        "[mcp_servers.kantaq]\n"
        'command = "kantaq"\n'
        'args = ["mcp", "stdio"]\n'
        f'env = {{ {MCP_STDIO_TOKEN_ENV} = "{TOKEN_PLACEHOLDER}" }}'
    )


def _stdio_snippets() -> list[AgentClientSnippet]:
    """Each client's stdio MCP config — the gateway spawned as a child (E09-T5).

    Unlike HTTP, stdio needs no live gateway URL: the client launches
    ``kantaq mcp stdio`` itself, which runs the exact same eight checks + audit
    over the pipe (E09-T4). The bearer rides the ``KANTAQ_MCP_TOKEN`` env var the
    child reads at startup; like the HTTP snippets, nothing here carries a real
    token — only the placeholder the web client substitutes locally.
    """
    json_config = _stdio_command_config()
    json_text = _mcp_json(json_config)
    codex_config: dict[str, Any] = {
        "mcp_servers": {
            "kantaq": {
                "command": "kantaq",
                "args": ["mcp", "stdio"],
                "env": {MCP_STDIO_TOKEN_ENV: TOKEN_PLACEHOLDER},
            }
        }
    }
    return [
        AgentClientSnippet(
            client="claude_code",
            label="Claude Code (stdio)",
            config=json_config,
            format=FORMAT_MCP_JSON,
            text=json_text,
            save_as=".mcp.json",
            setup=None,
            transport="stdio",
            instructions=(
                "save as .mcp.json — Claude Code launches `kantaq mcp stdio` itself; "
                f"replace {TOKEN_PLACEHOLDER} with your member token (it rides the "
                f"{MCP_STDIO_TOKEN_ENV} env var)"
            ),
        ),
        AgentClientSnippet(
            client="cursor",
            label="Cursor (stdio)",
            config=json_config,
            format=FORMAT_MCP_JSON,
            text=json_text,
            save_as=".cursor/mcp.json",
            setup=None,
            transport="stdio",
            instructions=(
                "save as .cursor/mcp.json — Cursor launches `kantaq mcp stdio` itself; "
                f"replace {TOKEN_PLACEHOLDER} with your member token"
            ),
        ),
        AgentClientSnippet(
            client="codex",
            label="Codex (stdio)",
            config=codex_config,
            format=FORMAT_TOML,
            text=_codex_stdio_toml(),
            save_as="~/.codex/config.toml",
            setup=None,
            transport="stdio",
            instructions=(
                "add this table to ~/.codex/config.toml — Codex launches "
                f"`kantaq mcp stdio` and passes {MCP_STDIO_TOKEN_ENV} from the env block; "
                f"replace {TOKEN_PLACEHOLDER} with your member token"
            ),
        ),
    ]


def _client_snippets(url: str) -> list[AgentClientSnippet]:
    """Every client's MCP config for a live gateway — both transports (E09-T5).

    The HTTP variants point at the live loopback ``url``; the stdio variants
    launch ``kantaq mcp stdio`` as a child (no URL needed). Both resolve to the
    same session derivation, the same eight checks, and the same audit (E09-T4) —
    a contract test pins that equivalence. A client picks whichever it speaks.
    The HTTP Claude Code config stays first so the back-compat ``snippet`` field
    keeps its meaning.
    """
    return _http_snippets(url) + _stdio_snippets()


@router.get("/agent-snippet", response_model=AgentSnippetOut)
def agent_snippet(actor: AnyActor, request: Request) -> AgentSnippetOut:
    settings: Settings = request.app.state.settings
    discovery = Path(settings.local_db_path).parent / "mcp.json"
    url = _live_gateway_url(discovery)

    if url is None:
        # stdio needs no live HTTP gateway — the client spawns `kantaq mcp stdio`
        # itself — so the stdio configs are always available; only the HTTP
        # variants need the discovered URL (E09-T5).
        return AgentSnippetOut(
            member_id=actor.member_id,
            gateway_url=None,
            gateway_live=False,
            token_placeholder=TOKEN_PLACEHOLDER,
            snippet=None,
            clients=_stdio_snippets(),
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
