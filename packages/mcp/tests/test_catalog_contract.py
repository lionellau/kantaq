"""The tool-schema contract gate (MOD-09, harness-standard rule 8).

"Contracts are tested, not assumed": the MCP tool catalog — names, verbs,
required actions, input and output schemas — is pinned against a checked-in
golden. Agents in the wild integrate against these schemas, so any drift must
be a deliberate, reviewed change, not a side effect.

To update the contract intentionally, regenerate the golden and commit it
with the change that justifies it:

    uv run python -c "
    import json, pathlib
    from kantaq_mcp.catalog import CATALOG
    dump = {s.name: {'title': s.title, 'description': s.description,
                     'verb': s.verb, 'collections': list(s.collections),
                     'required_action': s.required_action,
                     'input_schema': s.input_schema,
                     'output_schema': s.output_schema} for s in CATALOG}
    pathlib.Path('packages/mcp/tests/fixtures/tool_catalog.json').write_text(
        json.dumps(dump, indent=2, sort_keys=True) + '\\n', encoding='utf-8')
    "
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from jsonschema.validators import Draft202012Validator

from kantaq_mcp.catalog import CATALOG

GOLDEN = Path(__file__).parent / "fixtures" / "tool_catalog.json"


def _catalog_dump() -> dict[str, Any]:
    return {
        spec.name: {
            "title": spec.title,
            "description": spec.description,
            "verb": spec.verb,
            "collections": list(spec.collections),
            "required_action": spec.required_action,
            "input_schema": spec.input_schema,
            "output_schema": spec.output_schema,
        }
        for spec in CATALOG
    }


def test_catalog_matches_the_checked_in_contract() -> None:
    """Fails on any schema/verb/scope drift; see the module docstring to
    regenerate the golden when the change is intentional."""
    golden = json.loads(GOLDEN.read_text(encoding="utf-8"))
    assert _catalog_dump() == golden


@pytest.mark.parametrize("spec", CATALOG, ids=[s.name for s in CATALOG])
def test_schemas_are_valid_json_schema(spec: Any) -> None:
    Draft202012Validator.check_schema(spec.input_schema)
    Draft202012Validator.check_schema(spec.output_schema)


@pytest.mark.parametrize("spec", CATALOG, ids=[s.name for s in CATALOG])
def test_catalog_invariants(spec: Any) -> None:
    """Structural rules the gateway relies on, pinned for every future tool."""
    # Read tools must say what they read so agent.read aggregation has a ref.
    if spec.verb == "read":
        assert spec.read_ref is not None, "read tools must declare a read_ref"
    # Every tool names the identity action that gates its allowlist entry.
    assert spec.required_action
    # Inputs reject unknown keys — agents cannot smuggle extra arguments.
    assert spec.input_schema.get("additionalProperties") is False
