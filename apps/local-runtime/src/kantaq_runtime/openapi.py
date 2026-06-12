"""OpenAPI export for the typed TS client (E18-T2, D-08).

FastAPI emits the OpenAPI document; the web app's typed client is *generated*
from it, never hand-written. The document is checked in at
``web/src/api/openapi.json`` so the web toolchain needs no Python, and two
gates keep the boundary honest (the same drift pattern as the Supabase DDL):

- Python CI: ``test_openapi_drift`` regenerates the document and compares it
  byte-for-byte with the checked-in file — an API change cannot land without
  re-exporting.
- Web CI: regenerates ``schema.d.ts`` from the checked-in document with
  ``pnpm gen:api`` and fails on diff — the TS types cannot drift from the
  document they were generated from.

Rendering is deterministic: sorted keys, two-space indent, trailing newline.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from kantaq_runtime.app import create_app
from kantaq_runtime.config import Settings

# Repo-relative artifact path (consumed by `pnpm gen:api`).
OPENAPI_ARTIFACT = Path("web") / "src" / "api" / "openapi.json"


def generate_spec() -> dict[str, Any]:
    """The runtime's OpenAPI document, built from a config-independent app."""
    app = create_app(settings=Settings())
    return app.openapi()


def render_spec() -> str:
    return json.dumps(generate_spec(), indent=2, sort_keys=True) + "\n"


def find_repo_root(start: Path | None = None) -> Path:
    """Walk up to the uv workspace root (the directory holding ``web/``)."""
    current = (start or Path(__file__)).resolve()
    for candidate in [current, *current.parents]:
        marker = candidate / "pyproject.toml"
        if marker.is_file() and "tool.uv.workspace" in marker.read_text(encoding="utf-8"):
            return candidate
    raise RuntimeError("uv workspace root not found")


def read_artifact() -> str:
    return (find_repo_root() / OPENAPI_ARTIFACT).read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m kantaq_runtime.openapi",
        description="Export the runtime OpenAPI document for the TS client (D-08)",
    )
    parser.add_argument(
        "--check",
        action="store_true",
        help="exit non-zero if the checked-in document is stale instead of writing",
    )
    args = parser.parse_args(argv)

    rendered = render_spec()
    target = find_repo_root() / OPENAPI_ARTIFACT
    if args.check:
        if target.read_text(encoding="utf-8") != rendered:
            print(f"{target} is stale — run `uv run python -m kantaq_runtime.openapi`")
            return 1
        print(f"{target} is current")
        return 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(rendered, encoding="utf-8")
    print(f"wrote {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
