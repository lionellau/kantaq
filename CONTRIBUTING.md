# Contributing to kantaq

Thanks for helping build kantaq. This guide is short on purpose; the
authoritative planning (dev plan, sprints, and the per-module specs) is
maintained by the core team in a separate planning workspace.

## Ground rules

1. **Conventional Commits.** Every commit message follows
   [Conventional Commits](https://www.conventionalcommits.org/): `feat:`, `fix:`,
   `docs:`, `chore:`, `refactor:`, `test:`, `ci:`, … The commit-msg hook and CI
   enforce this.
2. **Tests ship with the code.** A task is not done without tests. Use the
   shared harness in `packages/test_harness` (builders, fakes, fixtures) and
   keep tests hermetic and deterministic — no real network, clock, or RNG.
3. **Keep the module spec in sync.** A behavior change with no spec update is not
   done. Update the relevant module spec alongside the change.
4. **Security gate.** Any change touching `packages/protocol`, the MCP gateway,
   or grants needs an adversarial + security review before merge.

## The Golden rule: reuse before build

Before writing implementation code for a module, evaluate whether an existing
open-source component can do part or all of the job. A candidate must clear **all**
of these:

- More than 5,000 GitHub stars.
- Actively maintained (a commit or release within ~3 months).
- Credible maintainers (a known backing org or well-known contributors).
- No known major or unpatched security advisory.
- A reuse-compatible license: MIT, Apache-2.0, BSD, ISC, or MPL-2.0 (avoid GPL/AGPL
  — kantaq ships under Apache-2.0).
- Fits the stack: Python 3.12 for core/backend, TypeScript + React for UI.

Find at least 3 candidates, pick one (or justify build-from-scratch), write a
2–3 line justification in the PR, and record the choice + license in
[`docs/stack.md`](docs/stack.md).

## Local setup

Prerequisites: Python 3.12, [`uv`](https://docs.astral.sh/uv/), Node ≥ 20,
[`pnpm`](https://pnpm.io/), `make`.

```bash
make setup        # uv sync + pnpm install + build web
make test         # pytest + Vitest
make lint         # ruff + Biome
make typecheck    # mypy + tsc
uv run pre-commit install   # enable git hooks
```

### Fast test loop (E27-T6)

`make test` runs pytest in **parallel** (`-n auto`, pytest-xdist) — ~2.5× faster
than serial on the full suite. Order is **randomized every run** (pytest-randomly,
our determinism guard); every run prints `[kantaq] pytest-randomly seed=<n>`, so
reproduce a failing order with `uv run pytest --randomly-seed=<n>`. Debugging one
file? Add `-n0` to run serially (skips the ~2-3s worker spawn) and `-p no:randomly`
to pin the order:

```bash
uv run pytest packages/core/tests/test_x.py -n0 -p no:randomly
```

Coverage is intentionally **off** the inner loop — `make test` is cov-free and fast.
The coverage gate (`make coverage`, protocol/mcp/core ≥ 90%) runs in CI and on demand.

### Postgres-gated tests locally

The backend / RLS / retention suites (`adapters/backend-supabase`, the SQLite↔Postgres
parity + metrics-calibration tests) need a **real Postgres** and otherwise skip
(`~108 tests`). To run them on your machine instead of waiting on a CI round-trip:

```bash
eval "$(scripts/local_postgres.sh start)"   # disposable postgresql@15 → exports KANTAQ_TEST_POSTGRES_URL
make test-pg                                  # the FULL suite, Postgres tests included
scripts/local_postgres.sh stop --purge        # tear it down
```

Needs `postgresql@15` (`brew install postgresql@15`). The cluster is created with
locale `C` so text ordering matches the parity assertions. Or point
`KANTAQ_TEST_POSTGRES_URL` at any reachable server yourself — that env var is the
only contract (CI provides it via a service container).

## Definition of Done (every change)

- Code and tests merged behind a reviewed PR; CI green.
- Tests follow the module's harness profile.
- Migrations have a verified rollback (when schema changes).
- The related module spec is updated.
- `docs/mcp.md` updated if an MCP tool changed.
