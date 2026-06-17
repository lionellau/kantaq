#!/usr/bin/env python
"""Role-based UAT — different user types against the real runtime API (NFR-E06-3).

The AI-backend 試錯 runner (``scripts/uat_ai_backend.py``) varies the *agent*
identity; this runner varies the *human* identity. It boots the real FastAPI
runtime on a hermetic SQLite arena, mints one bearer token per base role
(Owner / Maintainer / Member / Viewer + a scoped Agent token, plus the
unauthenticated case), and drives the same ``/v1/*`` endpoints the web UI calls
— asserting each HTTP outcome matches the permission matrix the runtime enforces
(``kantaq_core.identity.roles.can`` / ``ROLE_PERMISSIONS``, PRD §11 / FR-E06-7).

    uv run python scripts/uat_roles.py                 # print the matrix
    uv run python scripts/uat_roles.py --report out.md # also write a report

Expectations are *derived from* ``can`` rather than hand-listed, so the matrix
can never drift from the source of truth: a cell passes iff a 403 appears exactly
when ``can`` says the role may not do it (NFR-E06-3 — "permission checked on the
web/API surface"). Exits non-zero on any mismatch.

CI gate: the same property is enforced hermetically by
``tests/e2e/test_role_authz_matrix.py`` (run by the ``py`` workflow's pytest).
This script is the human-readable *reporter* over that property — the
``compat_check.py`` ↔ ``tests/compat`` idiom — for UAT/release evidence.

Hermetic: throwaway DB, ``HUB_MODE=local``, no Supabase, no real workspace.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class Probe:
    """One endpoint the web UI calls, tagged with the action it requires."""

    label: str
    method: str
    path: str  # may carry {ticket_id}, filled per run
    action: str  # the Action value the route's require_action() enforces
    body: dict | None = None


# The discriminating surface: tracker reads/writes, member admin, telemetry.
# Each row's `action` is the exact Action the runtime route enforces (confirmed
# in *_api.py), so `can(role, action)` is the oracle for the expected outcome.
PROBES: tuple[Probe, ...] = (
    Probe("read tickets", "GET", "/v1/tickets", "tickets.read"),
    Probe("read conflicts", "GET", "/v1/conflicts", "tickets.read"),
    Probe("read members", "GET", "/v1/members", "members.read"),
    Probe("read telemetry", "GET", "/v1/telemetry", "telemetry.read"),
    Probe(
        "create ticket", "POST", "/v1/tickets", "tickets.write", {"title": "role-uat ticket"}
    ),  # project_id filled at run
    Probe(
        "comment on ticket",
        "POST",
        "/v1/tickets/{ticket_id}/comments",
        "tickets.write",
        {"body": "role-uat comment"},
    ),
    Probe("change telemetry", "PUT", "/v1/telemetry", "telemetry.write", {"enabled": True}),
    Probe("invite a member", "POST", "/v1/members/invite", "members.invite", None),  # body per role
)

# Column order for the matrix. Agent is scoped by its token, not a role row.
PRINCIPALS = ("Owner", "Maintainer", "Member", "Viewer", "Agent")
AGENT_SCOPES = ("tickets.read", "proposals.write")


def _build_arena():  # noqa: ANN202 — internal
    """Boot the runtime on a throwaway DB; return (client, tokens, project_id, ticket_id).

    Mirrors scripts/e2e_server.py's boot (env before import → migrate → bootstrap
    Owner → seed a project + ticket), then mints a token for each other role.
    """
    data_dir = Path(tempfile.mkdtemp(prefix="kantaq-roles-uat-"))
    os.environ["LOCAL_DB_PATH"] = str(data_dir / "local.sqlite")
    os.environ["HUB_MODE"] = "local"
    os.environ.pop("KANTAQ_DB_URL", None)

    from sqlmodel import Session, select
    from starlette.testclient import TestClient

    from kantaq.cli import main as kantaq_cli
    from kantaq_core.identity import IdentityService, Role
    from kantaq_core.tracker.service import TrackerService
    from kantaq_db.models import Workspace
    from kantaq_db.session import get_engine, sqlite_url
    from kantaq_runtime.app import create_app
    from kantaq_runtime.auth import ensure_local_identity, keychain_for
    from kantaq_runtime.config import get_settings

    if kantaq_cli(["db", "migrate"]) != 0:
        raise SystemExit("roles-uat: migrations failed")

    settings = get_settings()
    engine = get_engine(sqlite_url(settings.local_db_path))
    owner_token = ensure_local_identity(engine, keychain_for(settings))
    if owner_token is None:
        raise SystemExit("roles-uat: expected a fresh database")

    tokens: dict[str, str] = {"Owner": owner_token}
    with Session(engine) as session:
        identity = IdentityService(session)
        owner_id = identity.list_members()[0].id
        for role, scopes in (
            (Role.maintainer, []),
            (Role.member, []),
            (Role.viewer, []),
            (Role.agent, list(AGENT_SCOPES)),
        ):
            minted = identity.invite(
                email=f"{role.value.lower()}@roles-uat.local", role=role, scopes=scopes
            )
            tokens[role.value] = minted.plaintext
        workspace_id = session.exec(select(Workspace)).one().id
        tracker = TrackerService(session, actor_id=owner_id, source="app")
        project = tracker.create_project(workspace_id=workspace_id, name="Roles UAT")
        ticket = tracker.create_ticket(project_id=project.id, title="Roles UAT ticket")
        project_id, ticket_id = project.id, ticket.id
        session.commit()

    client = TestClient(create_app(settings=settings, engine=engine))
    return client, tokens, project_id, ticket_id


def _expected_allowed(principal: str, action: str) -> bool:
    """The oracle: may this principal perform this action, per the runtime's own
    ``can``? Agent is checked against its token scopes; humans against their role."""
    from kantaq_core.identity.roles import Action, Role, can

    if principal == "Agent":
        return can(Role.agent, Action(action), scopes=list(AGENT_SCOPES))
    return can(Role(principal), Action(action))


def _call(client, probe: Probe, token: str | None, project_id: str, ticket_id: str) -> int:  # noqa: ANN001
    path = probe.path.format(ticket_id=ticket_id)
    body = probe.body
    if probe.label == "create ticket":
        body = {**probe.body, "project_id": project_id}  # type: ignore[dict-item]
    if probe.label == "invite a member":
        # unique email per caller so an allowed second inviter never 400s on a dup
        suffix = (token or "anon")[-6:]
        body = {"email": f"invitee-{suffix}@roles-uat.local", "role": "Member", "scopes": []}
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    resp = client.request(probe.method, path, json=body, headers=headers)
    return resp.status_code


def _render(
    date: str, rows: list[tuple[Probe, dict[str, tuple[bool, int, bool]]]], unauth: tuple[bool, int]
) -> str:
    lines = [f"# Role-based UAT — different user types vs the real API · {date}", ""]
    lines.append(
        "Each base role (and a scoped Agent token) drives the `/v1/*` endpoints the "
        "web UI calls; a cell passes (✓) iff the HTTP outcome matches the runtime's "
        "own `can()` matrix — a **403 appears exactly when** the role may not do it "
        "(NFR-E06-3). `allow` = permitted + got a non-403; `deny` = forbidden + got "
        "403. Generated by `scripts/uat_roles.py` on a hermetic arena."
    )
    lines.append("")
    header = "| action (required) | " + " | ".join(PRINCIPALS) + " |"
    lines.append(header)
    lines.append("|" + "---|" * (len(PRINCIPALS) + 1))
    for probe, cells in rows:
        parts = [f"{probe.label} (`{probe.action}`)"]
        for p in PRINCIPALS:
            allowed, status, ok = cells[p]
            verb = "allow" if allowed else "deny"
            mark = "✓" if ok else "✗"
            parts.append(f"{mark} {verb} ({status})")
        lines.append("| " + " | ".join(parts) + " |")
    lines.append("")
    ok_mark = "✓" if unauth[0] else "✗"
    lines.append(
        f"**Unauthenticated** (no token) on a protected route → {ok_mark} "
        f"401 bearer-token-required (got {unauth[1]})."
    )
    lines.append("")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="uat_roles", description=__doc__)
    parser.add_argument("--date", default=_dt.date.today().isoformat())
    parser.add_argument("--report", type=Path, default=None)
    args = parser.parse_args(argv)

    client, tokens, project_id, ticket_id = _build_arena()

    rows: list[tuple[Probe, dict[str, tuple[bool, int, bool]]]] = []
    total = passed = 0
    for probe in PROBES:
        cells: dict[str, tuple[bool, int, bool]] = {}
        for principal in PRINCIPALS:
            allowed = _expected_allowed(principal, probe.action)
            status = _call(client, probe, tokens[principal], project_id, ticket_id)
            ok = (status != 403) if allowed else (status == 403)
            cells[principal] = (allowed, status, ok)
            total += 1
            passed += ok
        rows.append((probe, cells))

    # The unauthenticated user: a protected route demands a token. Missing
    # credentials are 401 ("bearer token required", auth.py) — distinct from the
    # 403 a *known* role gets for an action it may not perform.
    unauth_status = _call(client, PROBES[0], None, project_id, ticket_id)
    unauth_ok = unauth_status == 401
    total += 1
    passed += unauth_ok

    print(f"\nRole-based UAT — user types vs the real API (hermetic) · {args.date}")
    print("  (✓ = HTTP outcome matches can(); a 403 appears exactly when the role may not)\n")
    head = f"  {'action':22} " + "".join(f"{p:>12}" for p in PRINCIPALS)
    print(head)
    for probe, cells in rows:
        line = f"  {probe.label:22} "
        for p in PRINCIPALS:
            allowed, status, ok = cells[p]
            line += f"{('✓' if ok else '✗') + ('A' if allowed else 'D') + str(status):>12}"
        print(line)
    print(f"\n  unauthenticated → {'✓' if unauth_ok else '✗'} {unauth_status} (expect 401)")

    verdict = "PASS" if passed == total else "FAIL"
    print(f"\nRole-based UAT: {passed} / {total} {verdict}  ·  {args.date}")
    if passed != total:
        print("\nA cell's HTTP outcome did not match can() — an authz hole or a drift. Fix it.")

    if args.report is not None:
        args.report.write_text(
            _render(args.date, rows, (unauth_ok, unauth_status)), encoding="utf-8"
        )
        print(f"\nWrote the role matrix to {args.report}")

    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
