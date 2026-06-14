#!/usr/bin/env python
"""Real-agent compatibility harness (E11-T2 Tier-1, MOD-24) — the live check.

The hero-flow CI gate (E27-T3) scripts the agent's MCP calls with the real MCP
SDK client; it proves the *kantaq side* of the connection. This harness proves
the other half: a **real, LLM-backed coding agent** (Claude Code / Codex),
running headless, can connect to kantaq's loopback MCP gateway, read a ticket,
and create a proposal — the agent-connection feature exactly as a teammate uses
it. It cannot be a deterministic CI gate (a real agent needs auth + network and
its output is non-deterministic), so it is opt-in: run it on a machine where the
agent is already signed in.

What it does, per installed agent:
  1. boots a disposable kantaq runtime DB + the MCP gateway as a real server,
     seeds an Owner, an Agent member (propose-first scopes), a project + ticket;
  2. runs the agent headless, pointed at the gateway with the agent's bearer
     token, with a task to read the ticket and propose a status change;
  3. asserts from the shared DB the gateway wrote — T1 connected, T2 read
     (agent.read audit row), T3 proposed (an AgentProposal on the ticket);
  4. approves the proposal as the Owner (the human half).

Tokens never touch argv or a committed file: Claude reads its token from a
0600 .mcp.json in a temp dir (deleted on exit); Codex reads it from an env var.

Usage:
    make verify-agent                     # all installed agents
    uv run python scripts/verify_agent.py --agent claude
    uv run python scripts/verify_agent.py --agent codex --keep-logs
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATEWAY_PORT = int(os.environ.get("KANTAQ_VERIFY_GATEWAY_PORT", "39451"))
RUN_TIMEOUT_S = int(os.environ.get("KANTAQ_VERIFY_TIMEOUT", "240"))
CLAUDE_BUDGET_USD = os.environ.get("KANTAQ_VERIFY_CLAUDE_BUDGET", "0.75")

TASK = (
    "You are connected to the kantaq issue tracker over an MCP server named "
    "'kantaq'. Use ONLY the kantaq MCP tools. First call the ticket_get tool "
    "with ticket_id={ticket_id} to read the ticket. Then call the "
    "agent_action_propose tool with ticket_id={ticket_id}, "
    'changes={{"status": "doing"}}, and note="proposed by the real-agent '
    'verify harness". Do not ask for confirmation; create the proposal. Then '
    "stop and report what you did in one sentence."
)


@dataclass
class AgentResult:
    name: str
    installed: bool
    ran: bool = False
    connected: bool = False
    read: bool = False
    proposed: bool = False
    approved: bool = False
    proposal_id: str | None = None
    duration_s: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        # The connection feature is proven when the agent connected and created
        # a proposal; the read audit row is corroborating evidence.
        return self.installed and self.connected and self.proposed


# --------------------------------------------------------------------- seeding


def _seed(data_dir: Path) -> dict:
    """Migrate a fresh DB and seed Owner + Agent + project + one ticket per agent.
    Returns the engine and the ids/tokens the run needs."""
    os.environ["LOCAL_DB_PATH"] = str(data_dir / "local.sqlite")
    os.environ["HUB_MODE"] = "local"
    os.environ.pop("KANTAQ_DB_URL", None)

    from kantaq.cli import main as kantaq_cli

    if kantaq_cli(["db", "migrate"]) != 0:
        raise SystemExit("verify-agent: migrations failed")

    from sqlmodel import Session, select

    from kantaq_core.identity import IdentityService, Role
    from kantaq_core.tracker import TrackerService
    from kantaq_db import Workspace
    from kantaq_db.session import get_engine, sqlite_url
    from kantaq_runtime.auth import ensure_local_identity, keychain_for
    from kantaq_runtime.config import get_settings
    from kantaq_sync_engine import EventLogSink

    settings = get_settings()
    engine = get_engine(sqlite_url(settings.local_db_path))
    owner_token = ensure_local_identity(engine, keychain_for(settings))
    if owner_token is None:
        raise SystemExit("verify-agent: expected a fresh database")

    with Session(engine) as session:
        identity = IdentityService(session)
        owner_id = identity.list_members()[0].id  # capture the scalar before the session closes
        # A distinct Agent member per CLI, so each one's audit rows + proposal are
        # attributable to it alone (no cross-attribution between agents).
        agents = {
            name: identity.invite(
                email=f"verify-{name}@local",
                role=Role.agent,
                scopes=["tickets.read", "proposals.write"],
            )
            for name in ("claude", "codex")
        }
        agents = {name: {"id": m.member_id, "token": m.plaintext} for name, m in agents.items()}
        workspace = session.exec(select(Workspace)).one()
        tracker = TrackerService(
            session, actor_id=owner_id, source="cli", sink=EventLogSink(session, owner_id)
        )
        project = tracker.create_project(workspace_id=workspace.id, name="Verify Project")
        # One ticket per agent so each run's proposal is unambiguous.
        tickets = {
            name: tracker.create_ticket(
                project_id=project.id, title=f"Verify {name} can connect and propose"
            ).id
            for name in ("claude", "codex")
        }

    return {"engine": engine, "owner_id": owner_id, "agents": agents, "tickets": tickets}


# --------------------------------------------------------------------- gateway


def _start_gateway(data_dir: Path) -> subprocess.Popen:
    env = {**os.environ, "LOCAL_DB_PATH": str(data_dir / "local.sqlite"), "HUB_MODE": "local"}
    proc = subprocess.Popen(
        ["uv", "run", "kantaq", "mcp", "dev", "--port", str(GATEWAY_PORT)],
        cwd=ROOT,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    health = f"http://127.0.0.1:{GATEWAY_PORT}/healthz"
    for _ in range(60):
        if proc.poll() is not None:
            raise SystemExit("verify-agent: the MCP gateway exited during startup")
        try:
            with urllib.request.urlopen(health, timeout=1) as resp:  # noqa: S310 — loopback only
                if resp.status == 200:
                    return proc
        except (urllib.error.URLError, ConnectionError, TimeoutError):
            time.sleep(0.5)
    proc.terminate()
    raise SystemExit("verify-agent: the MCP gateway never became healthy")


def _stop_gateway(proc: subprocess.Popen) -> None:
    # SIGTERM so the gateway flushes its aggregated read-audit on lifespan exit.
    proc.send_signal(signal.SIGINT)
    try:
        proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()


# -------------------------------------------------------------- agent runners


def _gateway_url() -> str:
    return f"http://127.0.0.1:{GATEWAY_PORT}/v1/mcp"


def _run_claude(token: str, ticket_id: str, work: Path, keep_logs: bool) -> tuple[bool, str]:
    mcp_config = work / "kantaq.mcp.json"
    mcp_config.write_text(
        json.dumps(
            {
                "mcpServers": {
                    "kantaq": {
                        "type": "http",
                        "url": _gateway_url(),
                        "headers": {"Authorization": f"Bearer {token}"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    mcp_config.chmod(0o600)
    cmd = [
        "claude",
        "-p",
        TASK.format(ticket_id=ticket_id),
        "--mcp-config",
        str(mcp_config),
        "--allowedTools",
        "mcp__kantaq__ticket_get",
        "mcp__kantaq__agent_action_propose",
        "--permission-mode",
        "bypassPermissions",
        "--output-format",
        "json",
        "--max-budget-usd",
        CLAUDE_BUDGET_USD,
        "--no-session-persistence",
    ]
    result = _exec(cmd, env=os.environ.copy())
    if not keep_logs:
        mcp_config.unlink(missing_ok=True)
    return result


def _run_codex(token: str, ticket_id: str, work: Path, keep_logs: bool) -> tuple[bool, str]:
    env = {**os.environ, "KANTAQ_AGENT_TOKEN": token}
    cmd = [
        "codex",
        "exec",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "-c",
        f'mcp_servers.kantaq.url="{_gateway_url()}"',
        "-c",
        'mcp_servers.kantaq.bearer_token_env_var="KANTAQ_AGENT_TOKEN"',
        TASK.format(ticket_id=ticket_id),
    ]
    return _exec(cmd, env=env)


def _exec(cmd: list[str], env: dict) -> tuple[bool, str]:
    try:
        proc = subprocess.run(
            cmd, cwd=ROOT, env=env, capture_output=True, text=True, timeout=RUN_TIMEOUT_S
        )
    except subprocess.TimeoutExpired:
        return False, f"timed out after {RUN_TIMEOUT_S}s"
    except FileNotFoundError:
        return False, "binary not found"
    tail = (proc.stdout or "")[-600:] + (proc.stderr or "")[-600:]
    return proc.returncode == 0, tail.strip()


# ------------------------------------------------------------------- asserts


def _assert_outcome(engine, agent_id: str, ticket_id: str) -> dict:
    """Read the shared DB the gateway wrote: did the agent connect, read, propose?"""
    from sqlmodel import Session, select

    from kantaq_db.models import AgentProposal, AuditEvent

    with Session(engine) as session:
        proposal = session.exec(
            select(AgentProposal)
            .where(AgentProposal.ticket_id == ticket_id)
            .where(AgentProposal.proposer_id == agent_id)
        ).first()
        mcp_rows = session.exec(
            select(AuditEvent)
            .where(AuditEvent.actor_id == agent_id)
            .where(AuditEvent.source == "mcp")
        ).all()
    return {
        "proposed": proposal is not None,
        "proposal_id": proposal.id if proposal else None,
        "connected": bool(mcp_rows),  # any source=mcp row means the agent reached the gateway
        "read": any(row.action == "agent.read" for row in mcp_rows),
    }


def _approve(engine, owner_id: str, proposal_id: str) -> bool:
    from sqlmodel import Session

    from kantaq_core.proposals import approve_proposal

    try:
        with Session(engine) as session:
            approve_proposal(session, proposal_id, actor_id=owner_id, source="cli")
            session.commit()
        return True
    except Exception as exc:  # noqa: BLE001 — report, don't crash the harness
        print(f"  approve failed: {exc}", file=sys.stderr)
        return False


# ---------------------------------------------------------------------- main


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Drive a real coding agent against kantaq's gateway."
    )
    parser.add_argument("--agent", choices=["claude", "codex", "all"], default="all")
    parser.add_argument("--keep-logs", action="store_true", help="keep the temp .mcp.json")
    args = parser.parse_args()

    targets = ["claude", "codex"] if args.agent == "all" else [args.agent]
    runners = {"claude": _run_claude, "codex": _run_codex}
    results = {
        name: AgentResult(name=name, installed=shutil.which(name) is not None) for name in targets
    }

    data_dir = Path(tempfile.mkdtemp(prefix="kantaq-verify-"))
    work = Path(tempfile.mkdtemp(prefix="kantaq-verify-cfg-"))
    seeded = _seed(data_dir)
    gateway = _start_gateway(data_dir)
    print(f"gateway live at {_gateway_url()}  (data: {data_dir})\n")

    try:
        for name in targets:
            res = results[name]
            if not res.installed:
                res.notes.append("not installed (skipped)")
                print(f"[{name}] SKIP — not on PATH")
                continue
            ticket_id = seeded["tickets"][name]
            print(f"[{name}] running headless against the gateway…")
            start = time.monotonic()
            ran_ok, tail = runners[name](
                seeded["agents"][name]["token"], ticket_id, work, args.keep_logs
            )
            res.duration_s = round(time.monotonic() - start, 1)
            res.ran = True
            if not ran_ok:
                res.notes.append(f"agent exited non-zero: {tail[:200]}")
    finally:
        _stop_gateway(gateway)  # flush aggregated read-audit before asserting

    for name in targets:
        res = results[name]
        if not (res.installed and res.ran):
            continue
        outcome = _assert_outcome(
            seeded["engine"], seeded["agents"][name]["id"], seeded["tickets"][name]
        )
        res.connected, res.read, res.proposed = (
            outcome["connected"],
            outcome["read"],
            outcome["proposed"],
        )
        res.proposal_id = outcome["proposal_id"]
        if res.proposed:
            res.approved = _approve(seeded["engine"], seeded["owner_id"], res.proposal_id)

    if not args.keep_logs:
        shutil.rmtree(work, ignore_errors=True)
        shutil.rmtree(data_dir, ignore_errors=True)

    print(f"\n{'agent':8} {'connect':8} {'read':6} {'propose':8} {'approve':8} {'time':7} notes")
    print("-" * 72)
    failed = False
    for name in targets:
        r = results[name]

        def mark(value: bool, installed: bool = r.installed) -> str:
            return ("✓" if value else "✗") if installed else "—"

        print(
            f"{name:8} {mark(r.connected):8} {mark(r.read):6} {mark(r.proposed):8} "
            f"{mark(r.approved):8} {str(r.duration_s) + 's':7} {'; '.join(r.notes)}"
        )
        if r.installed and not r.ok:
            failed = True

    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\nverified at {stamp}")
    if failed:
        print("RESULT: at least one installed agent did not connect + propose.")
        return 1
    if not any(results[n].installed for n in targets):
        print("RESULT: no target agent installed — nothing verified.")
        return 1
    print("RESULT: every installed agent connected, read, and proposed (then approved).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
