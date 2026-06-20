"""`kantaq mcp` CLI surface: the dev (HTTP) and stdio transports (E09 / E09-T4).

The parser accepts both subcommands, and ``cmd_mcp`` routes each to its serve
function — stdio to the stdin/stdout gateway, dev to the loopback HTTP one. The
serve functions themselves are unit-tested elsewhere (``serve_stdio`` fail-closed
startup in ``packages/mcp/tests/test_stdio.py``); here we pin the wiring so the
two transports never cross.
"""

from __future__ import annotations

from typing import Any

import pytest

from kantaq.cli import build_parser, cmd_mcp


def test_parser_accepts_dev_and_stdio() -> None:
    for sub in ("dev", "stdio"):
        args = build_parser().parse_args(["mcp", sub])
        assert args.mcp_command == sub
        assert args.func is cmd_mcp


def test_parser_rejects_an_unknown_mcp_subcommand() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["mcp", "telepathy"])


def _patch_common(monkeypatch: pytest.MonkeyPatch, calls: list[str]) -> None:
    monkeypatch.setattr("kantaq.cli._guard_schema", lambda: 0)
    monkeypatch.setattr("kantaq.cli._bootstrap_identity", lambda settings: None)
    monkeypatch.setattr("kantaq_db.session.get_engine", lambda url: object())
    monkeypatch.setattr("kantaq_mcp.gateway.Gateway", lambda *a, **k: "GW")
    monkeypatch.setattr("kantaq_mcp.stdio.serve_stdio", lambda gw, *a, **k: calls.append("stdio"))
    monkeypatch.setattr("kantaq_mcp.server.serve_gateway", lambda gw, *a, **k: calls.append("http"))


def test_mcp_stdio_routes_to_serve_stdio(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _patch_common(monkeypatch, calls)
    args = build_parser().parse_args(["mcp", "stdio"])
    assert cmd_mcp(args) == 0
    assert calls == ["stdio"]  # stdio, never the HTTP server


def test_mcp_dev_routes_to_serve_gateway(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[str] = []
    _patch_common(monkeypatch, calls)
    args = build_parser().parse_args(["mcp", "dev"])
    assert cmd_mcp(args) == 0
    assert calls == ["http"]


def test_mcp_stdio_surfaces_a_startup_auth_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    """A missing token makes ``serve_stdio`` raise ``StdioAuthError``; the CLI
    turns it into a clear non-zero exit, not a traceback."""
    from kantaq_mcp.stdio import StdioAuthError

    monkeypatch.setattr("kantaq.cli._guard_schema", lambda: 0)
    monkeypatch.setattr("kantaq.cli._bootstrap_identity", lambda settings: None)
    monkeypatch.setattr("kantaq_db.session.get_engine", lambda url: object())
    monkeypatch.setattr("kantaq_mcp.gateway.Gateway", lambda *a, **k: "GW")

    def _boom(gw: Any, *a: Any, **k: Any) -> None:
        raise StdioAuthError("set KANTAQ_MCP_TOKEN")

    monkeypatch.setattr("kantaq_mcp.stdio.serve_stdio", _boom)
    args = build_parser().parse_args(["mcp", "stdio"])
    assert cmd_mcp(args) == 1
