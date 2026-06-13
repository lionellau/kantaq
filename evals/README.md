# evals/

Context-quality evaluation set (MOD-21, Epic E16) — the hand-graded guard on the
role-aware context resolver (PRD §17.3). `kantaq eval` (also `make eval`) loads
and validates these fixtures, **scores** the resolver against them
(precision/recall over the 80 agent cells), and **fails on a drop over 5 points**
from the recorded baseline (`baseline.json`) — the FR-E16-5 CI gate. Record a
fresh baseline from a green tree with `kantaq eval --update-baseline`.

## Layout

- **`fixtures/memory.json`** — the shared memory pool every ticket draws from, plus
  `baseline_owner` (the actor whose view the `human_teammate` column represents).
  Each entry carries the fields the resolver's policy reads: `space`, `visibility`,
  `review_status`, `type`, `created_by`. The pool and tickets are **de-identified** —
  derived from a real, messy Linear export (the kind that surfaces resolver bugs
  synthetic data misses, PRD §17.3) with personal names, ticket IDs, product/domain
  names, and PR/commit refs removed; the grading is unchanged. `visibility`
  (local/team) and `review_status` (stale/rejected) are kantaq concepts assigned from
  real signals — e.g. a superseded domain is `stale`, a closed-and-replaced revision is
  `rejected`, personal working notes are `local`.
- **`fixtures/tickets/<id>.json`** — one ticket, its `candidate_memory` (pool entries
  offered to the resolver, marked linked/unlinked with a reason), and the expected
  `bundles` per graded role.

## The five roles

The four locked agent roles — `code_agent`, `qa_agent`, `design_agent`,
`product_agent` (FR-E16-1) — plus `human_teammate`, the precision/recall **baseline**
the agent bundles narrow from. `human_teammate` is graded but is **not** a resolver
policy: it is the device-owner's full view (team memory + their *own* local notes;
never another actor's local notes).

## Expected bundle = a complete partition

Every candidate is graded into exactly one of `must_include` / `must_exclude` /
`optional` (disjoint, and their union is the candidate set), with a one-line
`rationale`. `optional` entries are not scored. The validator enforces the partition
and the **NFR-E16-1** invariant: no agent bundle may include a `local` entry, and the
`human_teammate` baseline may include a `local` entry only if the baseline owner is
its author.

## Grading rubric

The decision procedure a grader follows for each (ticket, role) cell is documented in
`docs/modules/MOD-21-context-resolver-evals.md` (project-docs repo) under "Grading
rubric". E16-T4a graded the first 50 of 100 bundles; E16-T4b completes the set at
**20 tickets × 5 roles = 100 bundles** (the second 10 tickets, EVAL-11..20, extend the
shared pool and span the full lifecycle taxonomy, including an expired-entry case).

## Recommendation eval (E17-T3)

`reco_fixtures.json` holds 30 hand-graded `(stage, labels) → expected_roles` cases for
the MOD-22 recommendation engine. `kantaq_core.reco.confusion_matrix` scores
`recommend()` role-wise against them (a 2×2 confusion matrix); `test_reco_eval.py`
asserts it stays perfect (a deterministic engine, so any FP/FN is a real regression).
