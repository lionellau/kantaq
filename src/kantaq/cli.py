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

from kantaq import __version__

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
    ]
    mypy_cmd = [sys.executable, "-m", "mypy"]
    for pkg in packages:
        mypy_cmd += ["-p", pkg]
    rc = _run(mypy_cmd, cwd=root)
    if _have_web(root):
        rc = rc or _run(["pnpm", "-C", "web", "typecheck"], cwd=root)
    return rc


def cmd_db(args: argparse.Namespace) -> int:
    # Real migrations land in Epic E02 / MOD-02 (Alembic + SQLModel). The command
    # exists now so the bootstrap acceptance loop (setup -> dev -> migrate -> test)
    # is green from day one.
    print(f"kantaq db {args.db_command}: no migrations yet (implemented in Epic E02 / MOD-02)")
    return 0


def cmd_mcp(args: argparse.Namespace) -> int:
    # The loopback MCP gateway lands in Epic E09 / MOD-08.
    print(f"kantaq mcp {args.mcp_command}: not implemented yet (Epic E09 / MOD-08)")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    """Print the resolved config and verify the backend connection (MOD-14)."""
    from kantaq_runtime.config import HubMode, get_settings
    from kantaq_runtime.verify import verify_connection

    settings = get_settings()
    result = verify_connection(settings)
    print(f"hub_mode = {settings.hub_mode.value}")
    print(f"bind     = {settings.host}:{settings.port}")
    print(f"db_path  = {settings.local_db_path}")
    if settings.hub_mode is HubMode.supabase:
        print(f"supabase = {settings.supabase_url or '(unset)'}")
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

    typecheck = sub.add_parser("typecheck", help="run mypy + tsc")
    typecheck.set_defaults(func=cmd_typecheck)

    db = sub.add_parser("db", help="database migrations (E02)")
    db.add_argument("db_command", choices=["migrate", "downgrade"])
    db.set_defaults(func=cmd_db)

    mcp = sub.add_parser("mcp", help="MCP gateway (E09)")
    mcp.add_argument("mcp_command", choices=["dev"])
    mcp.set_defaults(func=cmd_mcp)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    func: Callable[[argparse.Namespace], int] = args.func
    return func(args)


if __name__ == "__main__":
    raise SystemExit(main())
