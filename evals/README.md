# evals/

Context-quality evaluation set (MOD-21, Epic E16) — the hand-graded guard on the
role-aware context resolver (PRD §17.3). `kantaq eval` (also `make eval`) loads
and validates these fixtures; the precision/recall run against the resolver lands
with the resolver in Sprint 4.

## Layout

- **`fixtures/memory.json`** — the shared memory pool every ticket draws from, plus
  `baseline_owner` (the actor whose view the `human_teammate` column represents).
  Each entry carries the fields the resolver's policy reads: `space`, `visibility`,
  `review_status`, `type`, `created_by`. The pool and tickets are **derived from the
  real JobWinAI V1 Linear export** (`docs/reference/JobWinAI_V1_Linear_Tickets.xlsx`
  in the project-docs repo) — real, messy tickets surface resolver bugs that synthetic
  data misses (PRD §17.3). The dataset's owner has cleared it for use in these public
  fixtures; bodies summarise real ticket descriptions and comments (real authors and
  PR/commit refs kept), with signed upload URLs dropped (they are auth tokens, not
  content). `visibility` (local/team) and `review_status` (stale/rejected) are kantaq
  concepts assigned from real signals — e.g. the superseded `jobwinai.com` domain is
  `stale`, the closed-and-replaced PR #124 is `rejected`, personal working notes are `local`.
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
rubric". E16-T4a grades the first 50 of 100 bundles (10 tickets × 5 roles) against the
real export; the remaining 10 tickets land in Sprint 4 (E16-T4b) using the same format.
