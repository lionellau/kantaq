.PHONY: help setup dev migrate test coverage lint typecheck eval mcp-dev clean

help: ## show this help
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | \
	  awk 'BEGIN {FS = ":.*?## "}; {printf "  make %-12s %s\n", $$1, $$2}'

setup: ## install both toolchains and build the web UI
	uv sync
	pnpm -C web install
	pnpm -C web build

dev: ## run the FastAPI runtime on 127.0.0.1:3939
	uv run kantaq dev

migrate: ## run database migrations (stub until Epic E02)
	uv run kantaq db migrate

test: ## run pytest + Vitest
	uv run kantaq test

coverage: ## run the coverage gate on protocol/mcp/core (>= 90%)
	uv run pytest --cov --cov-fail-under=90

lint: ## run ruff + Biome
	uv run kantaq lint

typecheck: ## run mypy + tsc
	uv run kantaq typecheck

eval: ## run context/reco evals (lands with MOD-21 / Epic E16)
	@echo "no evals yet (implemented in Epic E16 / MOD-21)"

mcp-dev: ## run the loopback MCP gateway (lands with Epic E09)
	uv run kantaq mcp dev

clean: ## remove build artifacts and caches
	rm -rf web/dist web/node_modules .venv \
	  .pytest_cache .mypy_cache .ruff_cache **/*.egg-info
