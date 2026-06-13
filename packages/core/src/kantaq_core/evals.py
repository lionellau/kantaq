"""The context-quality eval set: format, loader, and validator (MOD-21 / E16-T0).

The resolver is the product's highest-risk subsystem (PRD §17.3): a silent
regression that drops the right memory or admits the wrong memory degrades every
downstream agent action. The guard is a **hand-graded** fixture set — 20 tickets
× 5 roles = 100 expected context bundles — checked into ``evals/fixtures/``.

This module owns the on-disk **format** and the **validator** that keeps the
hand-graded fixtures honest (``kantaq eval``, this sprint). The precision/recall
runner that scores the resolver against these fixtures lands with the resolver in
Sprint 4 (MOD-21 "Continues in"); the format here is what it will consume.

Format (so a grader edits plain JSON, and so the next 10 tickets slot in):

* ``evals/fixtures/memory.json`` — the shared memory pool every ticket draws from,
  plus ``baseline_owner`` (the actor whose view the ``human_teammate`` column
  represents — they see team memory *and their own* ``local`` notes; nobody sees
  another actor's ``local`` notes, which never left that actor's device).
* ``evals/fixtures/tickets/<id>.json`` — one ticket, its candidate memory (drawn
  from the pool, marked linked/unlinked), and the expected bundle per graded role:
  ``must_include`` / ``must_exclude`` / ``optional`` (a complete partition of the
  candidates) with a one-line ``rationale``.

The five roles are the four locked agent roles (FR-E16-1) plus ``human_teammate``,
the precision/recall **baseline** the agent bundles narrow from (it is graded too,
but it is not a resolver policy — see :mod:`kantaq_core.memory_policy`).
"""

from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from kantaq_core import lifecycle, memory_policy
from kantaq_core.memory.service import MEMORY_SPACES, MEMORY_TYPES, REVIEW_STATUSES

# The five graded columns: the four resolver policies + the human baseline.
HUMAN_BASELINE_ROLE = "human_teammate"
EVAL_ROLES: tuple[str, ...] = (*memory_policy.ROLE_SLUGS, HUMAN_BASELINE_ROLE)

# The §17.3 targets. Half the bundles are graded this sprint (E16-T4a); the rest
# land in Sprint 4 (E16-T4b) — the format and validator already accept them.
TARGET_TICKETS = 20
TARGET_BUNDLES = TARGET_TICKETS * len(EVAL_ROLES)  # 100
SPRINT3_GRADED_TARGET = 50

_VISIBILITIES = ("local", "team")

# The fixed clock the eval set is graded and scored against. Grading is relative
# to one instant (the `expired` gate depends on it), so scoring must use the same
# instant or expiry-bearing fixtures would score differently than they were
# graded. Pinned, UTC, and injected into the resolver — never `datetime.now()`.
EVAL_NOW: datetime = datetime(2026, 6, 1, tzinfo=UTC)

# The eval gate (FR-E16-5, RISK-02): CI fails if precision or recall drops by more
# than this many points from the recorded baseline. Five points on a [0, 1] scale.
BASELINE_DROP_TOLERANCE = 0.05


class EvalFixtureError(ValueError):
    """A fixture file is malformed or internally inconsistent."""


@dataclass(frozen=True)
class EvalMemory:
    """One entry in the shared pool — the fields the resolver's policy reads."""

    id: str
    title: str
    space: str
    visibility: str
    review_status: str
    type: str  # noqa: A003 — mirrors the model field
    created_by: str
    body: str = ""
    expires_at: datetime | None = None


@dataclass(frozen=True)
class EvalCandidate:
    """A pool entry offered to one ticket's resolver, with its linkage."""

    memory_id: str
    linked: bool
    link_reason: str | None = None


@dataclass(frozen=True)
class ExpectedBundle:
    """The hand-graded ground truth for one (ticket, role) cell."""

    role: str
    must_include: tuple[str, ...]
    must_exclude: tuple[str, ...]
    optional: tuple[str, ...]
    rationale: str


@dataclass(frozen=True)
class EvalTicket:
    """One eval ticket: its work item, its candidate memory, its graded bundles."""

    id: str
    title: str
    description: str
    lifecycle_stage: str
    status: str
    labels: tuple[str, ...]
    candidates: tuple[EvalCandidate, ...]
    bundles: dict[str, ExpectedBundle]

    def candidate_ids(self) -> set[str]:
        return {candidate.memory_id for candidate in self.candidates}


@dataclass(frozen=True)
class EvalSet:
    """The whole fixture set: the memory pool, the baseline owner, the tickets."""

    memory: dict[str, EvalMemory]
    baseline_owner: str
    tickets: tuple[EvalTicket, ...]

    def graded_bundle_count(self) -> int:
        return sum(len(ticket.bundles) for ticket in self.tickets)


# --------------------------------------------------------------------- loading


def workspace_fixtures_dir(start: Path | None = None) -> Path:
    """Find ``<workspace-root>/evals/fixtures`` by walking up for the uv root."""
    current = (start or Path(__file__)).resolve()
    for candidate in [current, *current.parents]:
        pyproject = candidate / "pyproject.toml"
        if pyproject.is_file() and "tool.uv.workspace" in pyproject.read_text(encoding="utf-8"):
            return candidate / "evals" / "fixtures"
    raise EvalFixtureError("could not locate the uv workspace root above the eval fixtures")


def _parse_expires(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if not isinstance(raw, str):
        raise EvalFixtureError(f"expires_at must be an ISO string or null, got {raw!r}")
    return datetime.fromisoformat(raw)


def _load_memory_pool(path: Path) -> tuple[dict[str, EvalMemory], str]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    baseline_owner = raw.get("baseline_owner")
    if not isinstance(baseline_owner, str) or not baseline_owner:
        raise EvalFixtureError(f"{path.name} must name a string baseline_owner")
    pool: dict[str, EvalMemory] = {}
    for entry in raw["entries"]:
        memory = EvalMemory(
            id=entry["id"],
            title=entry["title"],
            space=entry["space"],
            visibility=entry["visibility"],
            review_status=entry["review_status"],
            type=entry["type"],
            created_by=entry["created_by"],
            body=entry.get("body", ""),
            expires_at=_parse_expires(entry.get("expires_at")),
        )
        if memory.id in pool:
            raise EvalFixtureError(f"duplicate memory id {memory.id!r} in {path.name}")
        pool[memory.id] = memory
    return pool, baseline_owner


def _load_ticket(path: Path) -> EvalTicket:
    raw = json.loads(path.read_text(encoding="utf-8"))
    ticket = raw["ticket"]
    candidates = tuple(
        EvalCandidate(
            memory_id=item["id"],
            linked=bool(item.get("linked", False)),
            link_reason=item.get("link_reason"),
        )
        for item in raw["candidate_memory"]
    )
    bundles: dict[str, ExpectedBundle] = {}
    for role, bundle in raw.get("bundles", {}).items():
        bundles[role] = ExpectedBundle(
            role=role,
            must_include=tuple(bundle.get("must_include", [])),
            must_exclude=tuple(bundle.get("must_exclude", [])),
            optional=tuple(bundle.get("optional", [])),
            rationale=bundle.get("rationale", ""),
        )
    return EvalTicket(
        id=ticket["id"],
        title=ticket["title"],
        description=ticket.get("description", ""),
        lifecycle_stage=ticket["lifecycle_stage"],
        status=ticket.get("status", ""),
        labels=tuple(ticket.get("labels", [])),
        candidates=candidates,
        bundles=bundles,
    )


def load_eval_set(base: Path | None = None) -> EvalSet:
    """Load the pool + every ``tickets/*.json`` into a typed :class:`EvalSet`."""
    base = base or workspace_fixtures_dir()
    pool, baseline_owner = _load_memory_pool(base / "memory.json")
    ticket_dir = base / "tickets"
    tickets = tuple(
        _load_ticket(path) for path in sorted(ticket_dir.glob("*.json")) if path.is_file()
    )
    return EvalSet(memory=pool, baseline_owner=baseline_owner, tickets=tickets)


# ------------------------------------------------------------------- validation


@dataclass(frozen=True)
class ValidationReport:
    """The outcome of :func:`validate`: problems (empty == valid) and counts."""

    problems: tuple[str, ...]
    ticket_count: int
    graded_bundles: int
    per_role: dict[str, int]

    @property
    def ok(self) -> bool:
        return not self.problems


def _validate_pool(pool: dict[str, EvalMemory]) -> list[str]:
    problems: list[str] = []
    for memory in pool.values():
        where = f"memory {memory.id!r}"
        if memory.space not in MEMORY_SPACES:
            problems.append(f"{where}: unknown space {memory.space!r}")
        if memory.visibility not in _VISIBILITIES:
            problems.append(f"{where}: unknown visibility {memory.visibility!r}")
        if memory.review_status not in REVIEW_STATUSES:
            problems.append(f"{where}: unknown review_status {memory.review_status!r}")
        if memory.type not in MEMORY_TYPES:
            problems.append(f"{where}: unknown type {memory.type!r}")
        if not memory.created_by:
            problems.append(f"{where}: missing created_by")
    return problems


def _validate_ticket(ticket: EvalTicket, evalset: EvalSet) -> list[str]:
    problems: list[str] = []
    where = f"ticket {ticket.id!r}"
    if ticket.lifecycle_stage not in lifecycle.STAGE_SLUGS:
        problems.append(f"{where}: unknown lifecycle_stage {ticket.lifecycle_stage!r}")

    candidate_ids = ticket.candidate_ids()
    if len(candidate_ids) != len(ticket.candidates):
        problems.append(f"{where}: duplicate candidate memory ids")
    for candidate in ticket.candidates:
        if candidate.memory_id not in evalset.memory:
            problems.append(f"{where}: candidate {candidate.memory_id!r} is not in the pool")
        if candidate.linked and not (candidate.link_reason or "").strip():
            problems.append(f"{where}: linked candidate {candidate.memory_id!r} needs a reason")

    for role, bundle in ticket.bundles.items():
        problems.extend(_validate_bundle(where, role, bundle, candidate_ids, evalset))
    return problems


def _validate_bundle(
    where: str,
    role: str,
    bundle: ExpectedBundle,
    candidate_ids: set[str],
    evalset: EvalSet,
) -> list[str]:
    problems: list[str] = []
    tag = f"{where} role {role!r}"
    if role not in EVAL_ROLES:
        problems.append(f"{tag}: not one of the five eval roles {EVAL_ROLES}")

    sets = {
        "must_include": set(bundle.must_include),
        "must_exclude": set(bundle.must_exclude),
        "optional": set(bundle.optional),
    }
    classified = sets["must_include"] | sets["must_exclude"] | sets["optional"]
    unknown = classified - candidate_ids
    if unknown:
        problems.append(f"{tag}: grades a non-candidate {sorted(unknown)}")
    # Disjoint and complete: every candidate is graded into exactly one bucket.
    total = sum(len(values) for values in sets.values())
    if total != len(classified):
        problems.append(f"{tag}: must_include/exclude/optional overlap")
    missing = candidate_ids - classified
    if missing:
        problems.append(f"{tag}: leaves candidates ungraded {sorted(missing)}")
    if not bundle.rationale.strip():
        problems.append(f"{tag}: missing rationale")

    # The SEC invariant (NFR-E16-1) at the fixture layer: an agent must never be
    # graded to *include* a local entry; the human baseline may include a local
    # entry only if it is the baseline owner's own (others' never reached them).
    for memory_id in bundle.must_include:
        memory = evalset.memory.get(memory_id)
        if memory is None or memory.visibility != "local":
            continue
        if role != HUMAN_BASELINE_ROLE:
            problems.append(f"{tag}: includes local entry {memory_id!r} (NFR-E16-1)")
        elif memory.created_by != evalset.baseline_owner:
            problems.append(
                f"{tag}: includes another actor's local entry {memory_id!r} "
                f"(owned by {memory.created_by!r}, baseline is {evalset.baseline_owner!r})"
            )
    return problems


def validate(base: Path | None = None) -> ValidationReport:
    """Load and validate the fixture set; an empty ``problems`` means valid."""
    evalset = load_eval_set(base)
    problems = _validate_pool(evalset.memory)
    for ticket in evalset.tickets:
        problems.extend(_validate_ticket(ticket, evalset))

    per_role: dict[str, int] = dict.fromkeys(EVAL_ROLES, 0)
    for ticket in evalset.tickets:
        for role in ticket.bundles:
            if role in per_role:
                per_role[role] += 1
    return ValidationReport(
        problems=tuple(problems),
        ticket_count=len(evalset.tickets),
        graded_bundles=evalset.graded_bundle_count(),
        per_role=per_role,
    )


# --------------------------------------------------------- scoring (E16-T3)
#
# Precision/recall of the rules-based resolver against the hand-graded ground
# truth (FR-E16-5, RISK-02). Only the four **agent** roles are scored — the
# ``human_teammate`` column is the baseline the agent bundles narrow from, not a
# resolver policy, so it has no resolver output to score (it is graded purely to
# anchor the SEC invariant in the fixtures). ``must_include`` drives recall,
# ``must_exclude`` drives precision, ``optional`` is unscored: a correct
# rules-based resolver therefore scores 1.0/1.0 (``must_exclude`` only ever
# covers a policy-gate failure), and this number is the regression baseline.

# A resolver: ``(role, candidates, *, now) -> object with an ``included`` tuple``.
# Injected so scoring stays decoupled from :mod:`kantaq_core.context` (and so a
# test can score a deliberately-broken resolver to prove the gate bites).
ResolveFn = Callable[..., Any]


@dataclass(frozen=True)
class RoleScore:
    """Precision/recall for one agent role over every ticket that grades it."""

    role: str
    cells: int
    true_positives: int
    false_positives: int
    false_negatives: int

    @property
    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom else 1.0

    @property
    def recall(self) -> float:
        denom = self.true_positives + self.false_negatives
        return self.true_positives / denom if denom else 1.0


@dataclass(frozen=True)
class Mismatch:
    """One cell where the resolver disagreed with the ground truth (diagnostics)."""

    ticket_id: str
    role: str
    false_positives: tuple[str, ...]  # included but graded must_exclude
    false_negatives: tuple[str, ...]  # graded must_include but dropped


@dataclass(frozen=True)
class ScoreReport:
    """Aggregate resolver score across every graded agent cell."""

    cells: int
    true_positives: int
    false_positives: int
    false_negatives: int
    per_role: tuple[RoleScore, ...]
    mismatches: tuple[Mismatch, ...]

    @property
    def precision(self) -> float:
        denom = self.true_positives + self.false_positives
        return self.true_positives / denom if denom else 1.0

    @property
    def recall(self) -> float:
        denom = self.true_positives + self.false_negatives
        return self.true_positives / denom if denom else 1.0


def _candidates_for(ticket: EvalTicket, pool: dict[str, EvalMemory]) -> list[EvalMemory]:
    # EvalMemory satisfies the read-only MemoryReadable protocol the resolver reads.
    return [pool[candidate.memory_id] for candidate in ticket.candidates]


def score(
    evalset: EvalSet,
    *,
    now: datetime = EVAL_NOW,
    resolve: ResolveFn | None = None,
) -> ScoreReport:
    """Score a resolver against the graded fixtures (precision/recall, agents only).

    ``resolve`` defaults to the production resolver
    (:func:`kantaq_core.context.resolve`); it is a parameter so a test can score a
    broken resolver and prove the baseline gate catches the drop.
    """
    if resolve is None:
        from kantaq_core import context  # lazy: keep the validator path resolver-free

        resolve = context.resolve

    tallies: dict[str, list[int]] = {role: [0, 0, 0, 0] for role in memory_policy.ROLE_SLUGS}
    mismatches: list[Mismatch] = []
    for ticket in evalset.tickets:
        candidates = _candidates_for(ticket, evalset.memory)
        for role in memory_policy.ROLE_SLUGS:
            expected = ticket.bundles.get(role)
            if expected is None:
                continue
            included = {entry.id for entry in resolve(role, candidates, now=now).included}
            must_in = set(expected.must_include)
            must_out = set(expected.must_exclude)
            false_pos = included & must_out
            false_neg = must_in - included

            cell = tallies[role]
            cell[0] += 1  # cells
            cell[1] += len(included & must_in)  # tp
            cell[2] += len(false_pos)  # fp
            cell[3] += len(false_neg)  # fn
            if false_pos or false_neg:
                mismatches.append(
                    Mismatch(
                        ticket_id=ticket.id,
                        role=role,
                        false_positives=tuple(sorted(false_pos)),
                        false_negatives=tuple(sorted(false_neg)),
                    )
                )

    per_role = tuple(
        RoleScore(role, c[0], c[1], c[2], c[3])
        for role, c in tallies.items()
        if c[0]  # only roles that some ticket graded
    )
    return ScoreReport(
        cells=sum(r.cells for r in per_role),
        true_positives=sum(r.true_positives for r in per_role),
        false_positives=sum(r.false_positives for r in per_role),
        false_negatives=sum(r.false_negatives for r in per_role),
        per_role=per_role,
        mismatches=tuple(mismatches),
    )


# ------------------------------------------------------- the baseline gate
#
# The first baseline is recorded from a green, reviewed run (the sprint-4 risk
# note: "the first baseline defines 'regression' forever after"). It is checked
# in beside the fixtures; ``kantaq eval`` compares every later run against it and
# fails CI on a drop over :data:`BASELINE_DROP_TOLERANCE` (FR-E16-5).


@dataclass(frozen=True)
class Baseline:
    """The recorded green-run score the eval gate compares against."""

    precision: float
    recall: float
    cells: int
    recorded_at: str
    note: str = ""


def baseline_path(base: Path | None = None) -> Path:
    """``evals/baseline.json`` (beside ``fixtures/``)."""
    base = base or workspace_fixtures_dir()
    return base.parent / "baseline.json"


def load_baseline(path: Path | None = None) -> Baseline | None:
    """Load the recorded baseline, or ``None`` if it has not been recorded yet."""
    path = path or baseline_path()
    if not path.is_file():
        return None
    raw = json.loads(path.read_text(encoding="utf-8"))
    return Baseline(
        precision=float(raw["precision"]),
        recall=float(raw["recall"]),
        cells=int(raw["cells"]),
        recorded_at=str(raw.get("recorded_at", "")),
        note=str(raw.get("note", "")),
    )


def write_baseline(
    report: ScoreReport,
    *,
    recorded_at: str,
    note: str = "",
    path: Path | None = None,
) -> Baseline:
    """Record ``report`` as the baseline (deterministic, sorted-key JSON)."""
    path = path or baseline_path()
    baseline = Baseline(
        precision=report.precision,
        recall=report.recall,
        cells=report.cells,
        recorded_at=recorded_at,
        note=note,
    )
    payload = {
        "precision": baseline.precision,
        "recall": baseline.recall,
        "cells": baseline.cells,
        "recorded_at": baseline.recorded_at,
        "note": baseline.note,
    }
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return baseline


def regressions_against_baseline(report: ScoreReport, baseline: Baseline) -> list[str]:
    """The structured reasons (empty == within tolerance) a run fails the gate."""
    problems: list[str] = []
    if report.precision < baseline.precision - BASELINE_DROP_TOLERANCE:
        problems.append(
            f"precision {report.precision:.3f} dropped > {BASELINE_DROP_TOLERANCE:.0%} "
            f"below the baseline {baseline.precision:.3f}"
        )
    if report.recall < baseline.recall - BASELINE_DROP_TOLERANCE:
        problems.append(
            f"recall {report.recall:.3f} dropped > {BASELINE_DROP_TOLERANCE:.0%} "
            f"below the baseline {baseline.recall:.3f}"
        )
    return problems
