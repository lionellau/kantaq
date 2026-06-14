"""Self-tests for the Compatibility profile helpers (MOD-30, E11-T2).

The gateway-driven contract test for ``FakeAgent`` is the Tier-1 compat suite
(``tests/compat``) — the same "the fake is exercised by the module that uses
it" rule FakeMCPClient follows. Here we pin only the pure decode helpers, which
have no gateway and must stay leaf-light.
"""

from __future__ import annotations

import pytest
from mcp.types import CallToolResult, TextContent

from kantaq_test_harness.compat import _decode, is_untrusted_wrapped


def _result(*, is_error: bool, structured: dict[str, object]) -> CallToolResult:
    return CallToolResult(
        content=[TextContent(type="text", text="")],
        structuredContent=structured,
        isError=is_error,
    )


def test_decode_success_carries_the_payload() -> None:
    call = _decode(_result(is_error=False, structured={"workspace": {"id": "w1"}}))
    assert call.ok is True
    assert call.code is None and call.message is None
    assert call.require() == {"workspace": {"id": "w1"}}


def test_decode_denial_surfaces_code_and_message() -> None:
    call = _decode(
        _result(is_error=True, structured={"error": {"code": "expiry", "message": "gone"}})
    )
    assert call.ok is False
    assert call.code == "expiry"
    assert call.message == "gone"
    with pytest.raises(AssertionError, match="expiry"):
        call.require()


def test_decode_tolerates_a_missing_or_malformed_error_shape() -> None:
    # isError with no structured error block: still a denial, code unknown.
    call = _decode(_result(is_error=True, structured={}))
    assert call.ok is False and call.code is None and call.message is None
    # A non-dict error block does not crash the decoder.
    weird = _decode(_result(is_error=True, structured={"error": "boom"}))
    assert weird.ok is False and weird.code is None


def test_untrusted_wrap_predicate() -> None:
    assert is_untrusted_wrapped('<untrusted source="ticket.title">hi</untrusted>')
    assert is_untrusted_wrapped('<untrusted source="x">hi</untrusted>\n')  # trailing ws tolerated
    assert not is_untrusted_wrapped("plain text")
    assert not is_untrusted_wrapped("hi </untrusted>")
