"""Hero-flow timing gate (E27-T2, stub).

Times the currently-available slice of the §1.1 hero loop and asserts it stays
under the 15-minute budget (§20.1). Today that slice is "verify config + boot the
runtime + serve the UI surface"; it expands as MCP, tickets, and the approval
queue land (E09 / E12 / E20). The gate's fail-closed behavior is unit-tested in
packages/test_harness/tests/test_hero_flow.py.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from kantaq_runtime.app import app
from kantaq_runtime.config import get_settings
from kantaq_runtime.verify import verify_connection
from kantaq_test_harness import HeroFlowTimer


def test_available_hero_slice_under_budget() -> None:
    with HeroFlowTimer() as timer:  # default 15-minute budget
        assert verify_connection(get_settings()).ok
        client = TestClient(app)
        assert client.get("/healthz").status_code == 200
        assert client.get("/").status_code == 200
    timer.assert_under_budget()
