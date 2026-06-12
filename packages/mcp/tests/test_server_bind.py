"""Loopback-only bind, random port, and the discovery file (E09-T1)."""

from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from kantaq_mcp.server import (
    MCP_PATH,
    GatewayBindError,
    GatewayBinding,
    bind_loopback_socket,
    write_discovery_file,
)


def test_binds_loopback_with_an_os_assigned_random_port() -> None:
    sock = bind_loopback_socket("127.0.0.1", 0)
    try:
        host, port = sock.getsockname()[:2]
        assert host == "127.0.0.1"
        assert port > 0, "port 0 must resolve to a real ephemeral port"
    finally:
        sock.close()


@pytest.mark.parametrize("host", ["0.0.0.0", "192.168.1.5", "::", "example.com"])
def test_refuses_non_loopback_hosts(host: str) -> None:
    with pytest.raises(GatewayBindError):
        bind_loopback_socket(host, 0)


def test_discovery_file_is_private_and_carries_no_secret(tmp_path: Path) -> None:
    target = tmp_path / "mcp.json"
    write_discovery_file(target, GatewayBinding(host="127.0.0.1", port=49152))

    mode = stat.S_IMODE(target.stat().st_mode)
    assert mode == 0o600, "discovery file must be owner-only, like the keychain"

    payload = json.loads(target.read_text(encoding="utf-8"))
    assert payload["url"] == f"http://127.0.0.1:49152{MCP_PATH}"
    assert payload["port"] == 49152
    # No secret material: the bearer token lives in the keychain only.
    assert "token" not in json.dumps(payload).lower()


def test_mcp_path_matches_the_module_spec() -> None:
    assert MCP_PATH == "/v1/mcp"
