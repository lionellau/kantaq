.PHONY: help setup dev migrate test coverage lint typecheck eval mcp-dev e2e clean

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

test: ## run pytest + Vitest
	uv run kantaq test

coverage: ## run the coverage gate on protocol/mcp/core (>= 90%)
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

clean: ## remove build artifacts and caches
	rm -rf web/dist web/node_modules .venv \
	  .pytest_cache .mypy_cache .ruff_cache **/*.egg-info
