"""The FastAPI application for the local runtime.

Bootstrap scope (Epic E01): bind nothing here (the CLI binds to 127.0.0.1), expose
``/healthz``, and serve the built web UI from ``web/dist`` when present. If the UI
has not been built yet, ``/`` returns a minimal placeholder so ``kantaq dev`` still
boots a working server (FR-E01-3). Real REST endpoints arrive with their epics.
"""

from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse, HTMLResponse, Response

from kantaq_runtime import __version__

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


def create_app() -> FastAPI:
    app = FastAPI(title="kantaq local runtime", version=__version__)

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok", "version": __version__}

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
