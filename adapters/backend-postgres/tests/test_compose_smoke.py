"""E25-T2: the docker-compose smoke — the stack comes up and a round-trip syncs.

The exit-criterion proof for the self-hosted backend (sprint-8 §Exit 1): a team
sets ``HUB_MODE=postgres``, runs ``docker compose up``, and syncs committed state
through the self-hosted sync-server with no Supabase. This test builds and starts
the real compose stack (Postgres + sync-server), bootstraps a member token with
the ``seed`` module, and round-trips commit → pull → snapshot through the HTTP
client.

It is **slow and opt-in** (it builds an image and boots containers), so it is
gated on ``KANTAQ_COMPOSE_SMOKE=1`` + a working Docker — out of the hermetic
``make test`` path, run in its own CI job (sprint-8 §Test harness: "tagged slow
and split").
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterator
from pathlib import Path

import httpx
import pytest

from kantaq_backend_postgres import SyncServerBackend
from kantaq_protocol import Event

SMOKE_ENV = "KANTAQ_COMPOSE_SMOKE"
COMPOSE_DIR = Path(__file__).resolve().parents[3] / "docker" / "self-hosted-backend"
HOST_PORT = "8899"
PROJECT = "kantaq-e25-smoke"


def _docker_ok() -> bool:
    try:
        return subprocess.run(["docker", "info"], capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


pytestmark = pytest.mark.skipif(
    os.environ.get(SMOKE_ENV) != "1" or not _docker_ok(),
    reason=f"set {SMOKE_ENV}=1 and start Docker to run the compose smoke",
)


def _compose(
    *args: str, env: dict[str, str], check: bool = True, **kw: object
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["docker", "compose", "-p", PROJECT, *args],
        cwd=COMPOSE_DIR,
        env=env,
        text=True,
        capture_output=True,
        check=check,
        **kw,  # type: ignore[arg-type]
    )


@pytest.fixture(scope="module")
def stack() -> Iterator[str]:
    env = {
        **os.environ,
        "SYNC_PORT": HOST_PORT,
        "POSTGRES_PASSWORD": "smoke-secret",
        "POSTGRES_USER": "kantaq",
        "POSTGRES_DB": "kantaq",
    }
    _compose("up", "-d", "--build", "--wait", env=env, timeout=900)
    try:
        yield f"http://localhost:{HOST_PORT}"
    finally:
        _compose("down", "-v", env=env, check=False, timeout=120)


def _seed(env_port: str) -> tuple[str, str]:
    """Bootstrap a member + token through the running server; return (member_id, token)."""
    env = {**os.environ, "SYNC_PORT": env_port, "POSTGRES_PASSWORD": "smoke-secret"}
    out = _compose(
        "exec",
        "-T",
        "sync-server",
        "uv",
        "run",
        "--no-dev",
        "python",
        "-m",
        "kantaq_backend_postgres.seed",
        "--email",
        "smoke@acme.dev",
        "--workspace",
        "Smoke",
        env=env,
        timeout=120,
    ).stdout
    member = token = ""
    for line in out.splitlines():
        if line.startswith("member:"):
            member = line.split(":", 1)[1].strip()
        elif line.startswith("token:"):
            token = line.split(":", 1)[1].strip()
    if not member or not token:
        raise AssertionError(f"seed did not print member + token:\n{out}")
    return member, token


def test_compose_stack_comes_up_and_round_trips(stack: str) -> None:
    # the stack is up
    assert httpx.get(f"{stack}/healthz", timeout=10).json() == {"status": "ok"}

    # bootstrap a member token through the running server's seed tool
    member_id, token = _seed(HOST_PORT)
    backend = SyncServerBackend(stack, token)

    # a real round-trip with no Supabase anywhere in the loop
    init = backend.session_init(sync_version=1, schema_version=1)
    assert init.sync_version >= 1

    event = Event(
        event_id="e" + "1".rjust(25, "0"),
        collection="tickets",
        entity_id="tkt_smoke0".ljust(26, "0"),
        actor_id=member_id,  # the server binds actor == the authenticated member
        actor_seq=1,
        op="patch",
        base_rev=None,
        policy_ref=None,
        payload={"title": "compose round-trip", "status": "todo"},
        sig=None,
    )
    out = backend.commit_events([event], require_signature=False)
    assert out[0].status == "committed"

    pulled = backend.pull("tickets")
    assert len(pulled) == 1
    snap = backend.snapshot("tickets")
    assert snap["tkt_smoke0".ljust(26, "0")]["title"] == "compose round-trip"
