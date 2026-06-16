"""A synthetic, shape-faithful Linear export for the E23-T3 importer (MOD-30).

The real JobWinAI Linear export is private (real names, real Linear URLs) and
must never enter the public repo (DEBT-17 / the no-private-data rule), so the CI
gate imports this **synthetic** export instead. It reproduces the JobWinAI
*shape* — the same counts (269 tickets, 185 parent links, 407 comments, 26
``[Epic]`` parents) and every named edge case (a Canceled ticket, an Epic with
children, multi-label rows, tickets with no estimate/due date, markdown bodies,
light comment threading, all five statuses) — with entirely fabricated content.
The real export is exercised separately as a local-only smoke (results recorded,
no data committed).

Deterministic (index-derived, no clock/RNG) so the import + round-trip gates are
reproducible. ``build_linear_export`` is parameterized so E27 can scale it down.
"""

from __future__ import annotations

from typing import Any

# JobWinAI's status histogram (sums to 269) — drives the realistic distribution.
_STATUS_PLAN: tuple[tuple[str, int], ...] = (
    ("Backlog", 117),
    ("Done", 144),
    ("In Review", 5),
    ("In Progress", 2),
    ("Canceled", 1),
)
_PRIORITIES = ("Urgent", "High", "Medium", "Low", "No priority")
_LABELS = (
    "Frontend",
    "Backend",
    "AI/Agents",
    "Infrastructure",
    "Security",
    "Feature",
    "Bug",
    "QA/Testing",
)
_ASSIGNEES = ("Avery Stone", "Blair Quinn", "Casey Vale")  # fabricated handles, not real people


def _statuses(n: int) -> list[str]:
    """``n`` statuses following the JobWinAI histogram, scaled to ``n``."""
    out: list[str] = []
    total = sum(c for _, c in _STATUS_PLAN)
    for status, count in _STATUS_PLAN:
        out.extend([status] * max(1, round(count * n / total)))
    # Trim/pad to exactly n, but never drop the lone Canceled (keep an edge case).
    if len(out) > n:
        out = out[:n]
        if "Canceled" not in out:
            out[-1] = "Canceled"
    while len(out) < n:
        out.append("Backlog")
    return out


def build_linear_export(
    *,
    tickets: int = 269,
    epics: int = 26,
    parent_links: int = 185,
    comments: int = 407,
) -> dict[str, Any]:
    """A synthetic Linear export dict (``{"tickets": [...], "comments": [...]}``)."""
    epics = min(epics, tickets)
    statuses = _statuses(tickets)
    ticket_rows: list[dict[str, Any]] = []
    epic_ids: list[str] = []

    for i in range(tickets):
        # A neutral synthetic id prefix — deliberately NOT the real export's
        # ticket-ID scheme (DEBT-17 lists Linear ticket IDs as sensitive).
        linear_id = f"LIN-{i + 1}"
        is_epic = i < epics
        if is_epic:
            epic_ids.append(linear_id)
        status = statuses[i]
        # Multi-label every 3rd ticket; markdown in some descriptions; no
        # due/estimate on the rest (the named edge cases).
        labels = [_LABELS[i % len(_LABELS)]]
        if i % 3 == 0:
            labels.append(_LABELS[(i + 3) % len(_LABELS)])
        description = (
            f"## Goal {i}\n\n- bullet one\n- bullet two\n\n`code()` and **bold**."
            if i % 4 == 0
            else f"Plain description {i}."
        )
        ticket_rows.append(
            {
                "id": linear_id,
                "title": f"[Epic] Workstream {i}" if is_epic else f"Task {i}: do the thing",
                "status": status,
                "priority": _PRIORITIES[i % len(_PRIORITIES)],
                # Edge cases: ~half carry no estimate / no due date.
                "estimate": (i % 5) if i % 2 == 0 else None,
                "assignee": _ASSIGNEES[i % len(_ASSIGNEES)] if i % 7 != 0 else None,
                "labels": labels,
                "parent": None,  # filled below
                "due_date": f"2026-0{(i % 9) + 1}-15T00:00:00Z" if i % 2 == 0 else None,
                "description": description,
            }
        )

    # 185 parent links: give the first `parent_links` non-epic tickets an Epic
    # parent (round-robin), so an Epic has children (the named edge case).
    children = [t for t in ticket_rows if not t["title"].startswith("[Epic]")]
    for n, child in enumerate(children[:parent_links]):
        child["parent"] = epic_ids[n % len(epic_ids)] if epic_ids else None

    comment_rows: list[dict[str, Any]] = []
    for j in range(comments):
        ticket = ticket_rows[j % len(ticket_rows)]
        # Light threading: every 4th comment replies to the previous author.
        reply_to = _ASSIGNEES[(j - 1) % len(_ASSIGNEES)] if j % 4 == 0 and j > 0 else None
        comment_rows.append(
            {
                "id": f"{ticket['id']}#c{j}",
                "ticket_id": ticket["id"],
                "author": _ASSIGNEES[j % len(_ASSIGNEES)],
                "reply_to": reply_to,
                "body": f"Comment {j} — **note** with `inline` and a [link](#)."
                if j % 3 == 0
                else f"Plain comment {j}.",
                "created": f"2026-03-{(j % 27) + 1:02d}T09:00:00Z",
            }
        )

    return {"tickets": ticket_rows, "comments": comment_rows}
