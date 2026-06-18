# evals/

The context-quality evaluation set — a hand-graded check on kantaq's role-aware
context resolver, the logic that decides which memory an agent sees for a given
ticket and role. `kantaq eval` (or `make eval`) scores the resolver against these
fixtures (precision/recall across the graded cases) and flags a meaningful drop
from the recorded baseline. Refresh the baseline from a green tree with
`kantaq eval --update-baseline`.

## What's here

- **`fixtures/memory.json`** — the shared memory pool every ticket draws from. Each
  entry carries the fields the resolver reads (`space`, `visibility`, `review_status`,
  `type`, `created_by`). The pool and tickets are **de-identified** — derived from a
  real, messy Linear export (the kind that surfaces resolver bugs synthetic data
  misses), with personal names, IDs, product/domain names, and PR/commit refs removed.
- **`fixtures/tickets/<id>.json`** — one ticket, the candidate memory offered to the
  resolver (each marked linked/unlinked with a reason), and the expected bundle per role.
- **`reco_fixtures.json`** — hand-graded cases for the recommendation engine
  (`(stage, labels) → expected roles`).

## The five roles

The four agent roles — `code_agent`, `qa_agent`, `design_agent`, `product_agent` — plus
`human_teammate`, the full device-owner view the agent bundles narrow down from. The
hard rule the validator enforces: an agent bundle never includes another person's
local-only notes.

## Grading

Each candidate is graded into exactly one of `must_include` / `must_exclude` /
`optional`, each with a one-line rationale (`optional` entries aren't scored). The full
decision procedure a grader follows lives in the project-docs repo.
