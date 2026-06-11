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

## Definition of Done (every change)

- Code and tests merged behind a reviewed PR; CI green.
- Tests follow the module's harness profile.
- Migrations have a verified rollback (when schema changes).
- The related module spec is updated.
- `docs/mcp.md` updated if an MCP tool changed.
