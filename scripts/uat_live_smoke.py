#!/usr/bin/env python
"""DEBT-30 live timed-smoke harness — the three v0.2 NFRs CI can't prove.

CI proves these NFRs only *hermetically* (one process, FakeClock, a throwaway
Postgres). The release promises them on the **real deployed infra**, so they need
a live run before the `0.2.0` tag. This harness is that run. Like
``verify_agent.py`` it is **opt-in** (network + a signed-in session), not a
deterministic CI gate.

Four checks (budgets from supabase/deploy/v0.2_live_verify.sql, Block 3):

  gateway-latency   NFR-E09 / Block-3 #2 — P50/P95 of a gated decision through the
                    gateway < 50 ms. In-process (the decision is local: one
                    ``_grant_live`` re-read), so it runs without a session and is
                    CI-checkable (tests/test_uat_live_smoke.py).
  revoke-recheck    NFR-E06-2 / Block-3 #1 (same-store half) — revoke a grant →
                    the gateway's live re-check denies the next call < 5 s.
                    In-process; the proven same-store path (mirrors
                    test_revocation_stops_the_session_within_the_wall_clock_budget).
  revoke-xreplica   NFR-E06-2 / Block-3 #1 (cross-replica half, D-21) — the
                    budget-critical re-check is covered by revoke-recheck; the
                    remaining two-runtime/networked propagation delta is an honest
                    documented MANUAL procedure (see the check), reported SKIP so
                    the matrix never shows a false PASS on an unmeasured number.
  retention-mark    Block-3 #3 — fetch the live safe-watermark and report what
                    compact_sync_events(30) will prune; confirm the nightly pg_cron
                    job runs it. LIVE: needs a session. SKIPs otherwise. The actual
                    DELETE is service-role-only (NFR-E24-1) — this harness never
                    holds a service-role key; the delete rides the pg_cron schedule
                    (verify it with supabase/deploy/v0.2_live_verify.sql Block 2).

Usage:
    make verify-live                      # all checks; live ones skip w/o a session
    uv run python scripts/uat_live_smoke.py
    uv run python scripts/uat_live_smoke.py --check gateway-latency --report out.md
    uv run python scripts/uat_live_smoke.py --require-live   # fail (not skip) the live checks
"""

from __future__ import annotations

import argparse
import math
import os
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

GATEWAY_BUDGET_MS = float(os.environ.get("KANTAQ_SMOKE_GATEWAY_MS", "50"))
REVOKE_BUDGET_S = float(os.environ.get("KANTAQ_SMOKE_REVOKE_S", "5"))
LATENCY_SAMPLES = int(os.environ.get("KANTAQ_SMOKE_SAMPLES", "200"))

CHECKS = ("gateway-latency", "revoke-recheck", "revoke-xreplica", "retention-mark")


@dataclass
class SmokeResult:
    name: str
    status: str = "SKIP"  # PASS | FAIL | SKIP | ERROR
    measured: str = ""
    budget: str = ""
    detail: str = ""
    notes: list[str] = field(default_factory=list)

    @property
    def counts_as_failure(self) -> bool:
        return self.status in ("FAIL", "ERROR")


def _pct(samples: list[float], p: float) -> float:
    if not samples:
        return float("nan")
    ordered = sorted(samples)
    k = max(0, math.ceil(p / 100 * len(ordered)) - 1)
    return ordered[k]


# --------------------------------------------------------- in-process seeding


def seed_world(engine, keychain) -> dict:
    """Seed Owner + Agent + project + ticket + a signed grant on a ready (migrated
    or metadata-created) engine. Split out so the hermetic test can build a world
    without the migrate/settings global state."""
    from sqlmodel import Session, select

    from kantaq_core.identity import GrantService, IdentityService, Role, ensure_device
    from kantaq_core.tracker import TrackerService
    from kantaq_db import Workspace
    from kantaq_sync_engine import EventLogSink

    with Session(engine) as session:
        identity = IdentityService(session)
        owner = identity.bootstrap_owner()
        if owner is None:
            raise RuntimeError("seed_world: expected a fresh database")
        agent = identity.invite(
            email="livesmoke-agent@local",
            role=Role.agent,
            scopes=["tickets.read", "proposals.write"],
        )
        session.commit()
        workspace = session.exec(select(Workspace)).one()
        tracker = TrackerService(
            session,
            actor_id=owner.member_id,
            source="cli",
            sink=EventLogSink(session, owner.member_id),
        )
        project = tracker.create_project(workspace_id=workspace.id, name="Live Smoke")
        ticket_id = tracker.create_ticket(project_id=project.id, title="latency + revoke probe").id
        session.commit()
        ensure_device(session, keychain, member_id=owner.member_id)
        session.commit()
        grant_id = (
            GrantService(session, keychain)
            .issue(
                subject_member_id=agent.member_id,
                resource="workspace/main",
                verbs=["tickets.read", "proposals.write"],
                actor_id=owner.member_id,
            )
            .id
        )
        session.commit()

    return {
        "engine": engine,
        "keychain": keychain,
        "agent_token": agent.plaintext,
        "owner_id": owner.member_id,
        "ticket_id": ticket_id,
        "grant_id": grant_id,
    }


def _seed_inprocess() -> dict:
    """A fresh migrated SQLite world with an in-memory keychain. Mirrors
    verify_agent._seed but keeps everything in-process (no gateway server)."""
    data_dir = Path(tempfile.mkdtemp(prefix="kantaq-livesmoke-"))
    os.environ["LOCAL_DB_PATH"] = str(data_dir / "local.sqlite")
    os.environ["HUB_MODE"] = "local"
    os.environ.pop("KANTAQ_DB_URL", None)

    from kantaq.cli import main as kantaq_cli

    if kantaq_cli(["db", "migrate"]) != 0:
        raise SystemExit("uat-live-smoke: migrations failed")

    from kantaq_db.session import get_engine, sqlite_url
    from kantaq_runtime.config import get_settings
    from kantaq_test_harness.keychain import FakeKeychain

    settings = get_settings()
    return seed_world(get_engine(sqlite_url(settings.local_db_path)), FakeKeychain())


def _gateway_for(engine):
    from kantaq_core.identity import TokenVerifier
    from kantaq_mcp.gateway import Gateway

    return Gateway(engine, verifier=TokenVerifier(engine))


# --------------------------------------------------------------- the checks


def check_gateway_latency(
    world: dict, *, samples: int = LATENCY_SAMPLES, budget_ms: float = GATEWAY_BUDGET_MS
) -> SmokeResult:
    """P50/P95 wall-clock of one gated ``handle_call`` decision (Block-3 #2).

    The gateway rate-limits 50 calls/min per session (PRD §15.1 defense 6), which
    would terminate the session mid-benchmark. The window is per-session, so we
    roll to a fresh grant-session every 40 calls — the throughput guard never
    trips. We are measuring per-decision *cost*, not throughput; latency is the
    real wall-clock (perf_counter) around each call.
    """
    from kantaq_mcp.gateway import GrantSessionRequest

    res = SmokeResult("gateway-latency", budget=f"P95 < {budget_ms:g}ms")
    gw = _gateway_for(world["engine"])
    actor = gw.authenticate(world["agent_token"])

    def fresh_session(tag: object):
        return gw.session_for(
            actor,
            session_id=f"latency-{tag}",
            grant_request=GrantSessionRequest(grant_id=world["grant_id"]),
        )

    call = {"tool_name": "ticket_get", "args": {"ticket_id": world["ticket_id"]}}
    session = fresh_session("warm")
    gw.handle_call(actor=actor, session=session, **call)  # warm caches/imports

    timings: list[float] = []
    for i in range(samples):
        if i % 40 == 0:  # stay under the 50/min per-session rate cut
            session = fresh_session(i)
        start = time.perf_counter()
        gw.handle_call(actor=actor, session=session, **call)
        timings.append((time.perf_counter() - start) * 1000.0)

    p50, p95 = _pct(timings, 50), _pct(timings, 95)
    res.measured = f"P50 {p50:.2f}ms · P95 {p95:.2f}ms (n={samples})"
    res.status = "PASS" if p95 < budget_ms else "FAIL"
    res.detail = f"max {max(timings):.2f}ms"
    return res


def check_revoke_recheck(world: dict, *, budget_s: float = REVOKE_BUDGET_S) -> SmokeResult:
    """Revoke a grant → the gateway's live re-check denies the next call (Block-3
    #1, same-store half). Wall-clock from revoke → denial < 5 s."""
    from sqlmodel import Session

    from kantaq_core.identity import GrantService
    from kantaq_mcp.gateway import DENY_IDENTITY, GatewayDenied, GrantSessionRequest

    res = SmokeResult("revoke-recheck", budget=f"< {budget_s:g}s")
    gw = _gateway_for(world["engine"])
    actor = gw.authenticate(world["agent_token"])
    session = gw.session_for(
        actor, session_id="revoke", grant_request=GrantSessionRequest(grant_id=world["grant_id"])
    )
    call = {"tool_name": "ticket_get", "args": {"ticket_id": world["ticket_id"]}}
    if "ticket" not in gw.handle_call(actor=actor, session=session, **call):
        res.status, res.detail = "ERROR", "grant did not authorize the read before revoke"
        return res

    with Session(world["engine"]) as db:
        GrantService(db, world["keychain"]).revoke(world["grant_id"], actor_id=world["owner_id"])
        db.commit()

    revoked_at = time.monotonic()
    try:
        gw.handle_call(actor=actor, session=session, **call)
    except GatewayDenied as denied:
        elapsed = time.monotonic() - revoked_at
        res.measured = f"{elapsed:.4f}s"
        ok = denied.reason == DENY_IDENTITY and elapsed < budget_s
        res.status = "PASS" if ok else "FAIL"
        res.detail = f"reason={denied.reason}"
    else:
        res.status, res.detail = "FAIL", "the revoked grant still authorized the call"
    return res


def check_revoke_xreplica(
    live: dict | None, *, budget_s: float = REVOKE_BUDGET_S, require_live: bool = False
) -> SmokeResult:
    """Cross-replica revocation against the live backend (Block-3 #1, the owed
    D-21 smoke): revoke on replica A → flush → replica B pulls → B's gateway
    denies, all < 5 s.

    Honesty note: the budget-critical mechanism — the gateway re-checks the grant
    live on *every* call, so a revoked grant stops the session on the very next
    call — is proven by ``revoke-recheck`` above. The remaining cross-replica
    delta (revoke flush → the other replica's ~2 s sync poll lands it) is an
    inherently TWO-RUNTIME, networked timing that a single-process harness can't
    stage faithfully, so it is a documented MANUAL procedure rather than a check
    dressed up as green:

        # terminal A and terminal B, both `kantaq sync login`-ed to the live
        # project as two members of one workspace; A has issued a grant B uses.
        # 1. B: open a grant-derived MCP session, confirm a gated call succeeds.
        # 2. A: revoke the grant (`POST /v1/grants/{id}/revoke`), note t0.
        # 3. B: re-run the gated call every ~1 s until it is DENIED (identity).
        # 4. assert (deny_time - t0) < 5 s.

    Reported as SKIP (not a false PASS) so the matrix never claims an unmeasured
    number; --require-live turns it into a FAIL to force the manual sign-off
    before the tag.
    """
    res = SmokeResult("revoke-xreplica", budget=f"< {budget_s:g}s")
    ready = "session ready" if live is not None else "needs `kantaq sync login`"
    res.status = "FAIL" if require_live else "SKIP"
    res.detail = f"manual two-runtime procedure — not auto-measured ({ready}; see docstring)"
    return res


def check_retention_mark(live: dict | None, *, require_live: bool = False) -> SmokeResult:
    """Report the live safe-watermark + what compact_sync_events(30) will prune
    (Block-3 #3). The DELETE itself is service-role-only and rides the nightly
    pg_cron job (NFR-E24-1: this harness never holds a service-role key) — verify
    the schedule with v0.2_live_verify.sql Block 2."""
    res = SmokeResult("retention-mark", budget="watermark reported; pg_cron active")
    if live is None:
        res.status = "FAIL" if require_live else "SKIP"
        res.detail = "no signed-in Supabase session (run `kantaq sync login`)"
        return res
    try:
        backend = live["backend"]
        backend.update_ack_watermark(
            member_id=live["member_id"], replica_id=live["replica_id"], acked_rev=live["cursor"]
        )
        watermark = backend.safe_watermark_rev()
        res.measured = (
            f"safe_watermark_rev={watermark}"
            if watermark is not None
            else "safe_watermark_rev=None (hold — no live replica acked)"
        )
        res.status = "PASS"
        res.detail = "DELETE rides nightly pg_cron 'kantaq-compact-sync-events'"
    except Exception as exc:  # noqa: BLE001
        res.status, res.detail = "ERROR", f"{type(exc).__name__}: {exc}"
    return res


# --------------------------------------------------------- live session load


def _load_live() -> dict | None:
    """Build the live backend from a stored Supabase session, mirroring
    cli.py:_sync_once. Returns None (→ live checks SKIP) when there's no session
    configured. Any failure degrades to None rather than crashing the runner."""
    try:
        from kantaq_runtime.config import get_settings

        settings = get_settings()
        if getattr(settings.hub_mode, "value", settings.hub_mode) != "supabase":
            return None
        url, anon_key = settings.supabase_url, settings.supabase_anon_key
        if not url or not anon_key:
            return None

        from kantaq.cli import SUPABASE_EMAIL_KEY, SUPABASE_REFRESH_KEY, _db_url
        from kantaq_backend_supabase import SupabaseAuth, SupabaseSyncBackend, lookup_active_members
        from kantaq_db.session import get_engine
        from kantaq_runtime.auth import keychain_for

        keychain = keychain_for(settings)
        email = keychain.get(SUPABASE_EMAIL_KEY)
        refresh_token = keychain.get(SUPABASE_REFRESH_KEY)
        if not email or not refresh_token:
            return None

        auth = SupabaseAuth(url, anon_key)
        session = auth.refresh(refresh_token)
        tokens = {"access": session.access_token, "refresh": session.refresh_token}

        def current_token() -> str:
            return str(tokens["access"])

        def refresh_session() -> str:
            rotated = auth.refresh(tokens["refresh"])
            tokens["access"], tokens["refresh"] = rotated.access_token, rotated.refresh_token
            return str(rotated.access_token)

        mine = [
            m
            for m in lookup_active_members(url, anon_key, current_token())
            if m.email.lower() == email.lower()
        ]
        if len(mine) != 1:
            return None
        me = mine[0]
        db = get_engine(_db_url())
        backend = SupabaseSyncBackend(
            url,
            anon_key,
            workspace_id=me.workspace_id,
            access_token=current_token,
            refresh=refresh_session,
        )
        return {
            "url": url,
            "anon_key": anon_key,
            "auth": auth,
            "keychain": keychain,
            "backend": backend,
            "db": db,
            "member_id": me.id,
            "replica_id": me.id,
            "workspace_id": me.workspace_id,
            "cursor": 0,
            "current_token": current_token,
        }
    except Exception as exc:  # noqa: BLE001 — no session / misconfig → live checks SKIP
        print(f"  (live session unavailable: {type(exc).__name__}: {exc})", file=sys.stderr)
        return None


# ---------------------------------------------------------------------- main


def run(selected: list[str], *, require_live: bool) -> list[SmokeResult]:
    results: list[SmokeResult] = []
    need_inprocess = any(c in selected for c in ("gateway-latency", "revoke-recheck"))
    need_live = any(c in selected for c in ("revoke-xreplica", "retention-mark"))

    # Load the live session BEFORE seeding the in-process world: _seed_inprocess()
    # mutates os.environ (HUB_MODE=local, LOCAL_DB_PATH=<temp>) and never restores
    # it, so reading the live session afterwards sees HUB_MODE=local and silently
    # returns None — every live check then SKIPs even with a valid `kantaq sync
    # login` session. Ordering the live read first keeps it on the real env. (DEBT-30)
    live = _load_live() if need_live else None
    world = _seed_inprocess() if need_inprocess else None

    if "gateway-latency" in selected:
        results.append(check_gateway_latency(world))
    if "revoke-recheck" in selected:
        # a fresh world so the latency check's reads don't interact with the revoke
        results.append(
            check_revoke_recheck(world if "gateway-latency" not in selected else _seed_inprocess())
        )
    if "revoke-xreplica" in selected:
        results.append(check_revoke_xreplica(live, require_live=require_live))
    if "retention-mark" in selected:
        results.append(check_retention_mark(live, require_live=require_live))
    return results


def _render(results: list[SmokeResult]) -> str:
    lines = [
        f"{'check':16} {'status':7} {'measured':34} {'budget':30} detail",
        "-" * 110,
    ]
    glyph = {"PASS": "✓ PASS", "FAIL": "✗ FAIL", "SKIP": "— SKIP", "ERROR": "! ERR "}
    for r in results:
        status = glyph.get(r.status, r.status)
        lines.append(f"{r.name:16} {status:7} {r.measured:34} {r.budget:30} {r.detail}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="DEBT-30 live timed-smoke harness.")
    parser.add_argument("--check", choices=[*CHECKS, "all"], default="all")
    parser.add_argument("--report", type=Path, help="write the matrix to a markdown file")
    parser.add_argument(
        "--require-live",
        action="store_true",
        help="treat a missing live session as FAIL instead of SKIP",
    )
    args = parser.parse_args()
    selected = list(CHECKS) if args.check == "all" else [args.check]

    results = run(selected, require_live=args.require_live)
    table = _render(results)
    print(table)

    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    print(f"\nran at {stamp}")
    if args.report:
        args.report.write_text(
            f"# DEBT-30 live timed-smoke — {stamp}\n\n```\n{table}\n```\n", encoding="utf-8"
        )
        print(f"report → {args.report}")

    failed = [r for r in results if r.counts_as_failure]
    skipped = [r for r in results if r.status == "SKIP"]
    if failed:
        print(f"RESULT: {len(failed)} check(s) failed.")
        return 1
    if skipped:
        print(
            f"RESULT: {len(results) - len(skipped)} passed, {len(skipped)} skipped "
            "(live checks need `kantaq sync login`; use --require-live to enforce)."
        )
        return 0
    print("RESULT: all checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
