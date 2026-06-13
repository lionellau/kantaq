"""Role-aware context resolver (MOD-21 / E16-T2).

A *context bundle* is what one agent role is allowed to read on one ticket: the
ticket plus its linked/in-scope memory, **filtered by the role's memory policy**
(:mod:`kantaq_core.memory_policy`). The resolver is the highest-risk subsystem
(PRD §17.3) — omit the right memory and every downstream agent action degrades;
admit the wrong memory and the privacy boundary (NFR-E16-1) breaks — so it is
deliberately small, pure, and rules-based, and it is graded against the
hand-graded eval set (:mod:`kantaq_core.evals`).

Two layers:

* :func:`resolve` — pure over a **given candidate list**. Applies the policy
  filter and shapes the result into a :class:`ContextBundle` (included entries,
  excluded-with-reason, the role's *missing* expected scopes, a token estimate).
  This is the function the eval set scores: a correct rules-based resolver hits
  precision = recall = 1.0, because the grading rubric applies the same gates the
  policy does (``must_exclude`` only ever covers a gate failure; an in-scope but
  tangential entry is graded ``optional`` and is unscored).
* :func:`resolve_for_ticket` — gathers the candidates for a live ticket from the
  store (linked memory ∪ in-scope-space memory), **team-visibility only**, then
  calls :func:`resolve`. Local entries are dropped at the gather seam *and* by the
  policy's privacy gate (defense in depth): an agent never learns even the
  *existence* of another actor's ``local`` note (NFR-E16-1).

The MCP tools ``role_context_get`` / ``role_context_preview`` (MOD-09) wrap this
and add untrusted-content fencing on every human-authored string (MOD-18); the
resolver itself returns raw domain objects so it stays testable without the
gateway.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime

from kantaq_core import memory_policy
from kantaq_core.memory_policy import MemoryReadable, PolicyDecision
from kantaq_db.models import MemoryEntry, Ticket

# Token-estimate heuristic: ~4 characters per token (the OpenAI rule of thumb for
# English prose). This is an *estimate* for the preview's budget signal, not a
# tokenizer — naming it honestly (no model-specific claim) keeps it stable across
# providers. A real per-model count is out of scope (the product runs no model).
_CHARS_PER_TOKEN = 4


@dataclass(frozen=True)
class ExcludedEntry:
    """One candidate the policy dropped, with the structured reason it was dropped.

    Carries the id and reason only — never the entry body — so the preview can
    explain an exclusion without re-leaking the content the policy withheld.
    """

    entry_id: str
    reason: str


@dataclass(frozen=True)
class ContextBundle:
    """What a role may read on a ticket: included memory + the reasoned remainder.

    ``included`` holds the actual entry objects (so a tool can render their
    fields); ``excluded`` and ``decisions`` are id+reason only.
    """

    role: str
    policy_id: str
    rationale: str
    included: tuple[MemoryReadable, ...]
    excluded: tuple[ExcludedEntry, ...]
    # Expected scopes (the policy's ``include_scopes``) that produced no included
    # entry — the "what context is missing" signal the preview surfaces.
    missing: tuple[str, ...]
    token_estimate: int
    decisions: tuple[PolicyDecision, ...]


def estimate_tokens(*texts: str) -> int:
    """A coarse token count for the preview budget (~4 chars/token, ceil)."""
    total = sum(len(text) for text in texts)
    return -(-total // _CHARS_PER_TOKEN)  # ceil division


def resolve(
    role: str,
    candidates: Sequence[MemoryReadable],
    *,
    now: datetime,
    extra_text: str = "",
) -> ContextBundle:
    """Resolve a context bundle for ``role`` over ``candidates`` (pure).

    Raises :class:`kantaq_core.memory_policy.UnknownAgentRoleError` for anything
    that is not one of the four locked agent roles (the human baseline is graded
    ground truth, never a resolver role). ``extra_text`` (e.g. the ticket body)
    is counted toward the token estimate but is not a memory candidate.
    """
    policy = memory_policy.policy_for(role)
    result = memory_policy.filter(list(candidates), policy, now=now)

    included = result.included
    excluded = tuple(ExcludedEntry(entry.id, reason) for entry, reason in result.excluded)

    # "missing" = the role's expected scopes that contributed nothing — a real
    # signal ("this role wants codebase context and there is none on this ticket").
    included_spaces = {entry.space for entry in included}
    missing = tuple(space for space in policy.include_scopes if space not in included_spaces)

    texts: list[str] = [extra_text]
    for entry in included:
        texts.append(getattr(entry, "title", "") or "")
        texts.append(getattr(entry, "body", "") or "")
    token_estimate = estimate_tokens(*texts)

    return ContextBundle(
        role=role,
        policy_id=policy.policy_id,
        rationale=policy.rationale,
        included=included,
        excluded=excluded,
        missing=missing,
        token_estimate=token_estimate,
        decisions=result.decisions,
    )


def gather_candidates(
    session: object,
    ticket_id: str,
    *,
    actor_id: str,
    spaces: Sequence[str],
    now: datetime,
) -> list[MemoryEntry]:
    """The live candidate set for a ticket: linked ∪ in-scope memory, team-only.

    Local entries are excluded *here* (not only by the policy's privacy gate) so
    an agent's resolver input never contains another actor's private note — its
    id can never reach the excluded list either (NFR-E16-1, gather seam).
    Expired entries are kept so the preview can report them as ``expired``.
    """
    # Imported lazily: the resolver core stays import-light for the eval path,
    # which drives :func:`resolve` directly with fixture rows (no DB session).
    from sqlmodel import Session

    from kantaq_core.memory.service import MemoryService

    assert isinstance(session, Session)
    service = MemoryService(session, actor_id=actor_id, source="mcp", now=lambda: now)

    by_id: dict[str, MemoryEntry] = {}
    for _link, entry in service.linked_memory(ticket_id, include_expired=True):
        if entry.visibility == "team":
            by_id[entry.id] = entry
    for space in spaces:
        for entry in service.list_entries(space=space, include_expired=True):
            if entry.visibility == "team":
                by_id.setdefault(entry.id, entry)
    return list(by_id.values())


def resolve_for_ticket(
    session: object,
    ticket: Ticket,
    role: str,
    *,
    actor_id: str,
    now: datetime,
) -> ContextBundle:
    """Resolve the bundle for a live ticket: gather candidates, then :func:`resolve`.

    The ticket's own human-authored text counts toward the token estimate; the
    caller (the MCP tool) fences both the ticket fields and each included entry.
    """
    policy = memory_policy.policy_for(role)
    candidates = gather_candidates(
        session,
        ticket.id,
        actor_id=actor_id,
        spaces=policy.include_scopes,
        now=now,
    )
    ticket_text = " ".join(
        part for part in (ticket.title, ticket.description, ticket.acceptance_criteria) if part
    )
    return resolve(role, candidates, now=now, extra_text=ticket_text)


__all__ = [
    "ContextBundle",
    "ExcludedEntry",
    "estimate_tokens",
    "gather_candidates",
    "resolve",
    "resolve_for_ticket",
]
