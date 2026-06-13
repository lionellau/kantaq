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
from dataclasses import dataclass
from datetime import datetime
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
