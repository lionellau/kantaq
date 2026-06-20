# kantaq self-hosted sync-server (MOD-28 / E25-T2).
# Build context is the repo root (see docker-compose.yml) so the uv workspace
# resolves. One image: the FastAPI sync-server that reuses the same validators
# as the Supabase backend — no second validator core.
FROM python:3.12-slim AS base

# uv (pinned digest-free tag is fine for a self-host image the team rebuilds).
COPY --from=ghcr.io/astral-sh/uv:0.7.19 /uv /usr/local/bin/uv

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    KANTAQ_SYNC_HOST=0.0.0.0 \
    KANTAQ_SYNC_PORT=8889

WORKDIR /app

# The workspace metadata + every member package the umbrella resolves. Copying
# the manifests first would let layer-cache the dependency install, but the
# workspace members are path deps, so we copy sources too.
COPY pyproject.toml uv.lock README.md ./
COPY packages ./packages
COPY adapters ./adapters
COPY apps ./apps
COPY src ./src

# Install the workspace (no dev tools). --frozen pins to the committed uv.lock.
RUN uv sync --frozen --no-dev

EXPOSE 8889

# create_schema runs on boot (idempotent); then uvicorn serves the app.
CMD ["uv", "run", "--no-dev", "python", "-m", "kantaq_backend_postgres.serve"]
