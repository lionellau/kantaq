"""The FastAPI application for the local runtime.

Bind nothing here (the CLI binds to 127.0.0.1); expose ``/healthz``, the
token-gated ``/v1/*`` API (E06), and serve the built web UI from ``web/dist``
when present. If the UI has not been built yet, ``/`` returns a minimal
placeholder so ``kantaq dev`` still boots a working server (FR-E01-3).

``create_app`` accepts an ``engine`` / ``verifier`` so tests run against a temp
database and a FakeClock-driven verifier; production resolves both lazily from
config on first API use.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, Response
from sqlalchemy.engine import Engine

from kantaq_core.identity import TokenVerifier
from kantaq_runtime import __version__
from kantaq_runtime.config import Settings, get_settings
from kantaq_runtime.members_api import router as members_router

_PLACEHOLDER_HTML = """<!doctype html>
<html lang="en">
<head><meta charset="utf-8"><title>kantaq</title></head>
<body style="font-family: system-ui, sans-serif; margin: 4rem auto; max-width: 40rem;">
  <h1>kantaq</h1>
  <p>The local runtime is up. The web UI is not built yet
     &mdash; run <code>make setup</code> (or <code>pnpm -C web build</code>) to build it.</p>
</body>
</html>
"""


def _web_dist() -> Path | None:
    """Locate a built ``web/dist`` directory by walking up from this file."""
    for parent in Path(__file__).resolve().parents:
        candidate = parent / "web" / "dist"
        if candidate.is_dir() and (candidate / "index.html").is_file():
            return candidate
    return None


def create_app(
    *,
    settings: Settings | None = None,
    engine: Engine | None = None,
    verifier: TokenVerifier | None = None,
) -> FastAPI:
    app = FastAPI(title="kantaq local runtime", version=__version__)
    app.state.settings = settings or get_settings()
    app.state.engine = engine
    app.state.verifier = verifier

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

    # API routes register before the SPA catch-all so /v1/* never falls
    # through to index.html. Auth lives on the routes (kantaq_runtime.auth).
    app.include_router(members_router)

    dist = _web_dist()

    @app.get("/{full_path:path}")
    def spa(full_path: str) -> Response:
        # Serve a built asset if it exists; otherwise fall back to index.html so
        # client-side routes (/memory, /agents, ...) deep-link instead of 404ing.
        # /healthz is registered first and keeps priority over this catch-all.
        if dist is not None:
            if full_path:
                candidate = (dist / full_path).resolve()
                if candidate.is_file() and dist.resolve() in candidate.parents:
                    return FileResponse(candidate)
            index = dist / "index.html"
            if index.is_file():
                return FileResponse(index)
        return HTMLResponse(_PLACEHOLDER_HTML)

    return app


app = create_app()
