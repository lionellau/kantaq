.PHONY: help setup dev migrate test test-pg coverage lint typecheck eval mcp-dev e2e compat verify-agent linkcheck clean

help: ## show this help
	@grep -E '^[a-zA-Z0-9_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  make %-12s %s\n", $$1, $$2}'

setup: ## install both toolchains and build the web UI
	uv sync
	pnpm -C web install
	pnpm -C web build

dev: ## run the FastAPI runtime on 127.0.0.1:3939
	uv run kantaq dev

migrate: ## run database migrations
	uv run kantaq db migrate

test: ## run pytest + Vitest (parallel: pytest -n auto; the fast inner loop)
	uv run kantaq test

test-pg: ## run the FULL suite incl. the Postgres-gated tests (needs a local Postgres; see CONTRIBUTING)
	@test -n "$$KANTAQ_TEST_POSTGRES_URL" || { \
	  echo "KANTAQ_TEST_POSTGRES_URL is unset — the Postgres tests would skip."; \
	  echo "Start a local Postgres and export it (see CONTRIBUTING 'Postgres-gated tests locally'):"; \
	  echo "  scripts/local_postgres.sh start   # prints the URL to export"; \
	  exit 1; }
	uv run pytest

coverage: ## run the coverage gate on protocol/mcp/core (>= 90%; parallel via addopts)
	uv run pytest --cov --cov-fail-under=90

lint: ## run ruff + Biome
	uv run kantaq lint

typecheck: ## run mypy + tsc
	uv run kantaq typecheck

eval: ## validate + score the context-eval set vs the baseline (MOD-21 / Epic E16)
	uv run kantaq eval

mcp-dev: ## run the loopback MCP gateway (random port; see docs/mcp.md)
	uv run kantaq mcp dev

e2e: ## run the Playwright hero-flow end-to-end (builds the UI first)
	pnpm -C web build
	pnpm -C web exec playwright install chromium
	pnpm -C web test:e2e

compat: ## run the scripted Tier-1 compatibility suite (T1-T8, MCP SDK client) + matrix line (E11-T2)
	uv run python scripts/compat_check.py

verify-agent: ## drive a REAL coding agent (Claude Code/Codex) against the gateway — opt-in, needs the agent signed in (E11-T2)
	uv run python scripts/verify_agent.py

linkcheck: ## spot-check external doc URLs at release time (opt-in; needs lychee)
	@command -v lychee >/dev/null 2>&1 || { echo "lychee not installed — see https://github.com/lycheeverse/lychee (brew install lychee / cargo install lychee). Internal links are already covered hermetically by 'make test'."; exit 1; }
	lychee --no-progress README.md CHANGELOG.md QUICKSTART.md docs

clean: ## remove build artifacts and caches
	rm -rf web/dist web/node_modules .venv \
	  .pytest_cache .mypy_cache .ruff_cache **/*.egg-info
