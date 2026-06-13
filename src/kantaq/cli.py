"""The ``kantaq`` command-line entrypoint.

A thin task dispatcher for the dev loop. It is intentionally small for the
v0.0.5 bootstrap (Epic E01); richer subcommands are added by the modules that
own them:

* ``kantaq dev``        boot the FastAPI runtime on 127.0.0.1:3939 (MOD-14)
* ``kantaq test``       run the Python (pytest) and web (Vitest) suites (MOD-30)
* ``kantaq lint``       ruff + Biome
* ``kantaq typecheck``  mypy + tsc
* ``kantaq db migrate`` (stub until MOD-02 / Epic E02)
* ``kantaq mcp dev``    (stub until MOD-08 / Epic E09)

Every command exits non-zero on failure so it can gate CI.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING

from kantaq import __version__

if TYPE_CHECKING:
    from kantaq_backend_supabase import SupabaseAuth
    from kantaq_core.identity import Keychain
    from kantaq_runtime.config import Settings

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 3939


def find_root(start: Path | None = None) -> Path:
    """Walk up from ``start`` (or cwd) to the uv workspace root."""
    current = (start or Path.cwd()).resolve()
    for candidate in [current, *current.parents]:
        pyproject = candidate / "pyproject.toml"
        if pyproject.is_file() and "tool.uv.workspace" in pyproject.read_text(encoding="utf-8"):
            return candidate
    return current


def _run(cmd: list[str], cwd: Path | None = None) -> int:
    print(f"$ {' '.join(cmd)}", file=sys.stderr)
    return subprocess.run(cmd, cwd=cwd).returncode


def _have_web(root: Path) -> bool:
    if not (root / "web" / "package.json").is_file():
        return False
    if shutil.which("pnpm") is None:
        print("pnpm not found on PATH; cannot run web tasks", file=sys.stderr)
        return False
    return True


def cmd_dev(args: argparse.Namespace) -> int:
    """Boot the FastAPI runtime. ``--check`` boots, hits /healthz, then exits.

    Resolves host/port from config (``--host``/``--port`` override) and verifies
    the backend connection before serving (MOD-14 / E22-T2).
    """
    import uvicorn

    from kantaq_runtime.app import app
    from kantaq_runtime.config import get_settings
    from kantaq_runtime.verify import verify_connection

    settings = get_settings()
    host = args.host or settings.host
    port = args.port or settings.port

    result = verify_connection(settings)
    if not result.ok:
        print(f"connection verify failed: {result.message}", file=sys.stderr)
        return 1
    print(f"verify: {result.message}", file=sys.stderr)

    schema_rc = _guard_schema()
    if schema_rc != 0:
        return schema_rc

    _bootstrap_identity(settings)

    if not args.check:
        uvicorn.run(app, host=host, port=port)
        return 0

    import threading

    import httpx

    config = uvicorn.Config(app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    ok = False
    deadline = time.time() + 30
    url = f"http://{host}:{port}/healthz"
    while time.time() < deadline:
        if getattr(server, "started", False):
            try:
                if httpx.get(url, timeout=2.0).status_code == 200:
                    ok = True
                    break
            except httpx.HTTPError:
                pass
        time.sleep(0.2)

    server.should_exit = True
    thread.join(timeout=10)
    print("dev smoke: OK" if ok else "dev smoke: FAILED", file=sys.stderr)
    return 0 if ok else 1


def cmd_test(args: argparse.Namespace) -> int:
    root = find_root()
    rc = _run([sys.executable, "-m", "pytest"], cwd=root)
    if rc != 0:
        return rc
    if _have_web(root):
        rc = _run(["pnpm", "-C", "web", "test"], cwd=root)
    return rc


def cmd_eval(args: argparse.Namespace) -> int:
    """Validate the context-eval fixtures (MOD-21 / Epic E16).

    This sprint runs the fixture validator and reports grading coverage; exits
    non-zero on any inconsistency so CI can gate it. The precision/recall run
    against the resolver lands with the resolver itself in Sprint 4 (MOD-21).
    """
    from kantaq_core import evals

    base = find_root() / "evals" / "fixtures"
    try:
        report = evals.validate(base)
    except evals.EvalFixtureError as exc:
        print(f"kantaq eval: {exc}", file=sys.stderr)
        return 1

    print(
        f"kantaq eval: {report.ticket_count} ticket(s), "
        f"{report.graded_bundles}/{evals.TARGET_BUNDLES} bundles graded "
        f"(Sprint-3 target {evals.SPRINT3_GRADED_TARGET})"
    )
    for role, count in report.per_role.items():
        print(f"  {role:16} {count} graded", file=sys.stderr)
    if not report.ok:
        print(f"kantaq eval: {len(report.problems)} problem(s):", file=sys.stderr)
        for problem in report.problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("kantaq eval: fixtures valid (precision/recall lands with the resolver, Sprint 4)")
    return 0


def cmd_lint(args: argparse.Namespace) -> int:
    root = find_root()
    rc = _run([sys.executable, "-m", "ruff", "check", "."], cwd=root)
    rc_fmt = _run([sys.executable, "-m", "ruff", "format", "--check", "."], cwd=root)
    rc = rc or rc_fmt
    if _have_web(root):
        rc = rc or _run(["pnpm", "-C", "web", "lint"], cwd=root)
    return rc


def cmd_typecheck(args: argparse.Namespace) -> int:
    root = find_root()
    packages = [
        "kantaq",
        "kantaq_protocol",
        "kantaq_sync_engine",
        "kantaq_core",
        "kantaq_mcp",
        "kantaq_db",
        "kantaq_runtime",
        "kantaq_backend_supabase",
        "kantaq_test_harness",
    ]
    mypy_cmd = [sys.executable, "-m", "mypy"]
    for pkg in packages:
        mypy_cmd += ["-p", pkg]
    rc = _run(mypy_cmd, cwd=root)
    if _have_web(root):
        rc = rc or _run(["pnpm", "-C", "web", "typecheck"], cwd=root)
    return rc


def _db_url() -> str:
    """The local replica URL: ``KANTAQ_DB_URL`` override, else the configured SQLite."""
    import os

    from kantaq_db.session import sqlite_url
    from kantaq_runtime.config import get_settings

    override = os.environ.get("KANTAQ_DB_URL")
    if override:
        return override
    return sqlite_url(get_settings().local_db_path)


def cmd_db(args: argparse.Namespace) -> int:
    """Alembic migrations, seed, schema guard, and dialect parity (MOD-02)."""
    from kantaq_db import migrations, parity, schema_version
    from kantaq_db.seed import seed_demo
    from kantaq_db.session import get_engine

    url = _db_url()
    command = args.db_command

    if command == "migrate":
        migrations.upgrade(url)
        print(
            f"kantaq db migrate: schema at revision {schema_version.HEAD_REVISION} "
            f"(version {schema_version.EXPECTED_SCHEMA_VERSION})"
        )
        return 0
    if command == "downgrade":
        migrations.downgrade(url, "-1")
        print("kantaq db downgrade: rolled back one revision")
        return 0
    if command == "seed":
        summary = seed_demo(get_engine(url))
        verb = "seeded" if summary.created else "already present"
        print(
            f"kantaq db seed: demo workspace {verb} — "
            f"{summary.projects} project(s), {summary.tickets} ticket(s), "
            f"{summary.comments} comment(s)"
        )
        return 0
    if command == "check":
        check = schema_version.verify(get_engine(url))
        print(f"kantaq db check: {check.message}")
        return 0 if check.ok else 1
    if command == "check-parity":
        ok, message = parity.check_parity()
        print(f"kantaq db check-parity: {message}")
        return 0 if ok else 1
    return 0


def _guard_schema() -> int:
    """Verify the local schema before serving; return non-zero to refuse boot.

    FR-E02-4: the runtime refuses to start on a schema it does not understand.
    An uninitialized database is a clear "run `kantaq db migrate`" instruction.
    """
    from kantaq_db import schema_version
    from kantaq_db.session import get_engine

    check = schema_version.verify(get_engine(_db_url()))
    if not check.ok:
        print(f"refusing to start: {check.message}", file=sys.stderr)
        return 1
    print(f"schema: {check.message}", file=sys.stderr)
    return 0


def _bootstrap_identity(settings: Settings) -> None:
    """First boot: mint the Owner token and park it in the keychain (E06, D-06).

    Solo mode has no human login, but the API is still token-gated, so the
    first boot creates the local Owner. Idempotent on every later boot.
    """
    from kantaq_db.session import get_engine
    from kantaq_runtime.auth import ensure_device_identity, ensure_local_identity, keychain_for

    engine = get_engine(_db_url())
    keychain = keychain_for(settings)
    minted = ensure_local_identity(engine, keychain)
    if minted is not None:
        print(
            "first boot: minted the local Owner token (run `kantaq token show`)",
            file=sys.stderr,
        )
    # E06-T4: every boot ensures the runtime's Ed25519 device identity — the
    # seed in the keychain, the verify key registered (and synced) as a
    # devices row. Idempotent; prints nothing on later boots.
    ensure_device_identity(engine, keychain)


def cmd_token(args: argparse.Namespace) -> int:
    """Local token management against the keychain + database (MOD-06).

    `show` prints the runtime token the keychain holds; `rotate` revokes the
    keychain holder's tokens, mints a fresh one, and re-parks it. Anyone with
    shell access to this machine already owns the local profile (D-06), so
    these commands do not themselves require the token.
    """
    from sqlmodel import Session

    from kantaq_core.identity import IdentityService, TokenVerifier
    from kantaq_db.session import get_engine
    from kantaq_runtime.auth import RUNTIME_TOKEN_KEY, ensure_local_identity, keychain_for
    from kantaq_runtime.config import get_settings

    settings = get_settings()
    keychain = keychain_for(settings)
    engine = get_engine(_db_url())

    if args.token_command == "show":
        token = keychain.get(RUNTIME_TOKEN_KEY)
        if token is None:
            print("no runtime token in the keychain; run `kantaq dev` once", file=sys.stderr)
            return 1
        print(token)
        return 0

    # rotate: revoke whatever the keychain token belongs to and mint fresh.
    minted = ensure_local_identity(engine, keychain)
    if minted is not None:
        print("no identity existed yet; minted the first Owner token instead", file=sys.stderr)
        return 0
    current = keychain.get(RUNTIME_TOKEN_KEY)
    actor = TokenVerifier(engine).verify(current) if current else None
    if actor is None:
        print(
            "keychain token is missing or already revoked; cannot identify the member. "
            "Rotate via the API instead (POST /v1/members/{id}/rotate).",
            file=sys.stderr,
        )
        return 1
    with Session(engine) as session:
        fresh = IdentityService(session).rotate_token(actor.member_id)
    keychain.set(RUNTIME_TOKEN_KEY, fresh.plaintext)
    print("token rotated; the old token is revoked (run `kantaq token show`)", file=sys.stderr)
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    """Run the loopback MCP gateway (E09 / MOD-08).

    Binds 127.0.0.1 on a random port by default (`LOCAL_MCP_PORT=auto`),
    publishes the bound URL on stdout and in a 0600 discovery file beside the
    database (`mcp.json`, no secrets), and serves the v0.0.5 tool catalog.
    Agents authenticate with a member bearer token (`kantaq token show`).
    """
    from kantaq_db.session import get_engine
    from kantaq_mcp.gateway import Gateway
    from kantaq_mcp.server import GatewayBindError, serve_gateway
    from kantaq_runtime.config import get_settings

    settings = get_settings()

    schema_rc = _guard_schema()
    if schema_rc != 0:
        return schema_rc
    _bootstrap_identity(settings)

    host = args.host or settings.local_mcp_host
    if args.port is not None:
        port = args.port
    else:
        port = 0 if settings.local_mcp_port == "auto" else int(settings.local_mcp_port)

    engine = get_engine(_db_url())
    discovery = Path(settings.local_db_path).parent / "mcp.json"
    try:
        serve_gateway(Gateway(engine), host=host, port=port, discovery_path=discovery)
    except GatewayBindError as exc:
        print(f"kantaq mcp dev: {exc}", file=sys.stderr)
        return 1
    return 0


# Keychain slots for the member's Supabase session (E24-T4). Sits beside the
# runtime token in the same 0600 keychain; tokens never print or log.
SUPABASE_EMAIL_KEY = "supabase-session-email"
SUPABASE_ACCESS_KEY = "supabase-access-token"
SUPABASE_REFRESH_KEY = "supabase-refresh-token"


def cmd_sync(args: argparse.Namespace) -> int:
    """Online sync against the configured Supabase backend (E24-T4, MOD-05).

    ``login`` exchanges an emailed one-time code for a session (kept in the
    keychain). ``once`` runs one push + pull cycle through the MOD-04 sync
    engine. ``status`` reports local sync state without touching the network.
    The acting member is resolved from the backend's members mirror by the
    session's verified email — RLS scopes everything else.
    """
    from kantaq_backend_supabase import AuthError, SupabaseAuth, SyncBackendError
    from kantaq_runtime.auth import keychain_for
    from kantaq_runtime.config import HubMode, get_settings

    settings = get_settings()
    if args.sync_command == "status":
        return _sync_status(settings)
    if settings.hub_mode is not HubMode.supabase:
        print(
            f"kantaq sync {args.sync_command}: HUB_MODE={settings.hub_mode.value} has no "
            "shared backend (set HUB_MODE=supabase; see docs/setup-supabase.md)",
            file=sys.stderr,
        )
        return 1
    if not settings.supabase_url or not settings.supabase_anon_key:
        print("SUPABASE_URL and SUPABASE_ANON_KEY are required", file=sys.stderr)
        return 1

    url, anon_key = settings.supabase_url, settings.supabase_anon_key
    keychain = keychain_for(settings)
    auth = SupabaseAuth(url, anon_key)
    try:
        if args.sync_command == "login":
            return _sync_login(auth, keychain, args.email)
        return _sync_once(url, anon_key, auth, keychain)
    except (AuthError, SyncBackendError) as exc:
        print(f"kantaq sync {args.sync_command}: {exc}", file=sys.stderr)
        return 1


def _sync_login(auth: SupabaseAuth, keychain: Keychain, email: str) -> int:
    auth.request_magic_link(email)
    code = input(f"enter the code emailed to {email}: ").strip()
    session = auth.verify(email, code)
    keychain.set(SUPABASE_EMAIL_KEY, session.user.email)
    keychain.set(SUPABASE_ACCESS_KEY, session.access_token)
    keychain.set(SUPABASE_REFRESH_KEY, session.refresh_token)
    print(f"signed in as {session.user.email}", file=sys.stderr)
    return 0


def _sync_once(url: str, anon_key: str, auth: SupabaseAuth, keychain: Keychain) -> int:
    from kantaq_backend_supabase import SupabaseSyncBackend, lookup_active_members
    from kantaq_db.session import get_engine
    from kantaq_sync_engine import SyncEngine

    email = keychain.get(SUPABASE_EMAIL_KEY)
    refresh_token = keychain.get(SUPABASE_REFRESH_KEY)
    if not email or not refresh_token:
        print("no Supabase session; run `kantaq sync login --email you@team.dev`", file=sys.stderr)
        return 1

    # Rotate the session up front so the access token is fresh for the run,
    # and keep a refresh hook for the (rare) mid-run expiry.
    session = auth.refresh(refresh_token)
    keychain.set(SUPABASE_ACCESS_KEY, session.access_token)
    keychain.set(SUPABASE_REFRESH_KEY, session.refresh_token)
    tokens = {"access": session.access_token, "refresh": session.refresh_token}

    def current_token() -> str:
        return str(tokens["access"])

    def refresh_session() -> str:
        rotated = auth.refresh(tokens["refresh"])
        tokens["access"] = rotated.access_token
        tokens["refresh"] = rotated.refresh_token
        keychain.set(SUPABASE_ACCESS_KEY, rotated.access_token)
        keychain.set(SUPABASE_REFRESH_KEY, rotated.refresh_token)
        return str(rotated.access_token)

    mine = [
        member
        for member in lookup_active_members(url, anon_key, current_token())
        if member.email.lower() == email.lower()
    ]
    if not mine:
        print(
            f"no active member row for {email} on the backend; ask the maintainer "
            "to add you (docs/setup-supabase.md, team manifest)",
            file=sys.stderr,
        )
        return 1
    if len(mine) > 1:
        workspaces = ", ".join(sorted(member.workspace_id for member in mine))
        print(
            f"{email} is active in more than one workspace ({workspaces}); "
            "multi-workspace sync arrives after v0.0.5",
            file=sys.stderr,
        )
        return 1

    me = mine[0]
    backend = SupabaseSyncBackend(
        url,
        anon_key,
        workspace_id=me.workspace_id,
        access_token=current_token,
        refresh=refresh_session,
    )
    engine = SyncEngine(get_engine(_db_url()), backend, actor_id=me.id)
    pushed = engine.push()
    pulled = engine.pull()
    print(
        f"push: {pushed.committed} committed, {pushed.already_known} already known "
        f"(of {pushed.submitted} pending) · pull: {pulled.applied} applied, "
        f"{pulled.own_reconciled} own reconciled · cursor {pulled.cursor}"
    )
    return 0


def _sync_status(settings: Settings) -> int:
    from sqlmodel import Session, col, func, select

    from kantaq_db import EventLog, SyncCursor
    from kantaq_db.session import get_engine
    from kantaq_runtime.auth import keychain_for

    keychain = keychain_for(settings)
    email = keychain.get(SUPABASE_EMAIL_KEY)
    with Session(get_engine(_db_url())) as session:
        pending = session.exec(
            select(func.count()).select_from(EventLog).where(col(EventLog.committed_rev).is_(None))
        ).one()
        cursors = session.exec(select(SyncCursor)).all()
    print(f"hub_mode = {settings.hub_mode.value}")
    print(f"session  = {email or '(not signed in)'}")
    print(f"pending  = {pending} event(s) awaiting push")
    for cursor in cursors:
        print(f"cursor   = {cursor.collection}: {cursor.acked_rev} (actor {cursor.actor_id})")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Print the resolved config and verify the backend connection (MOD-14)."""
    from kantaq_db import schema_version
    from kantaq_db.session import get_engine
    from kantaq_runtime.config import HubMode, get_settings
    from kantaq_runtime.verify import verify_connection

    settings = get_settings()
    result = verify_connection(settings)
    schema = schema_version.verify(get_engine(_db_url()))
    print(f"hub_mode = {settings.hub_mode.value}")
    print(f"bind     = {settings.host}:{settings.port}")
    print(f"db_path  = {settings.local_db_path}")
    if settings.hub_mode is HubMode.supabase:
        print(f"supabase = {settings.supabase_url or '(unset)'}")
    print(f"schema   = {schema.status}: {schema.message}")
    print(f"verify   = {'OK' if result.ok else 'FAIL'}: {result.message}")
    return 0 if result.ok else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="kantaq", description="kantaq dev CLI")
    parser.add_argument("--version", action="version", version=f"kantaq {__version__}")
    sub = parser.add_subparsers(dest="command", required=True)

    dev = sub.add_parser("dev", help="boot the FastAPI runtime")
    dev.add_argument(
        "--host", default=None, help=f"override bind host (default: HOST or {DEFAULT_HOST})"
    )
    dev.add_argument(
        "--port",
        type=int,
        default=None,
        help=f"override bind port (default: PORT or {DEFAULT_PORT})",
    )
    dev.add_argument("--check", action="store_true", help="boot, hit /healthz, exit")
    dev.set_defaults(func=cmd_dev)

    doctor = sub.add_parser("doctor", help="print config + verify backend connection")
    doctor.set_defaults(func=cmd_doctor)

    test = sub.add_parser("test", help="run pytest + Vitest")
    test.set_defaults(func=cmd_test)

    lint = sub.add_parser("lint", help="run ruff + Biome")
    lint.set_defaults(func=cmd_lint)

    ev = sub.add_parser("eval", help="validate context-eval fixtures (E16)")
    ev.set_defaults(func=cmd_eval)

    typecheck = sub.add_parser("typecheck", help="run mypy + tsc")
    typecheck.set_defaults(func=cmd_typecheck)

    db = sub.add_parser("db", help="database migrations (E02)")
    db.add_argument(
        "db_command",
        choices=["migrate", "downgrade", "seed", "check", "check-parity"],
    )
    db.set_defaults(func=cmd_db)

    token = sub.add_parser("token", help="local runtime token (E06)")
    token.add_argument("token_command", choices=["show", "rotate"])
    token.set_defaults(func=cmd_token)

    mcp = sub.add_parser("mcp", help="MCP gateway (E09)")
    mcp.add_argument("mcp_command", choices=["dev"])
    mcp.add_argument(
        "--host", default=None, help="override bind host (loopback only; default: LOCAL_MCP_HOST)"
    )
    mcp.add_argument(
        "--port",
        type=int,
        default=None,
        help="override bind port (default: LOCAL_MCP_PORT, auto = random)",
    )
    mcp.set_defaults(func=cmd_mcp)

    sync = sub.add_parser("sync", help="online sync with the team backend (E24)")
    sync.set_defaults(func=cmd_sync)
    sync_sub = sync.add_subparsers(dest="sync_command", required=True)
    sync_login = sync_sub.add_parser("login", help="sign in with an emailed one-time code")
    sync_login.add_argument("--email", required=True, help="your invited member email")
    sync_sub.add_parser("once", help="run one push + pull cycle")
    sync_sub.add_parser("status", help="local sync state (no network)")

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func: Callable[[argparse.Namespace], int] = args.func
    return func(args)


if __name__ == "__main__":
    raise SystemExit(main())
