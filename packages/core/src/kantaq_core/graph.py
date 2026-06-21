"""The dependency graph, folded on demand from ``ticket_relationships`` (MOD-29 / E15-T2).

Not a new table: the "what blocks what" graph is materialized from the v0.1
``ticket_relationships`` rows (MOD-03) — the **blocks** family (``blocking`` /
``blocked-by``), the same arcs the relation engine's cycle guard runs over.
``related`` / ``duplicate`` are symmetric (no direction) and excluded from
pathing — they are not dependencies.

D-27 (the locked cycle rule): the graph is acyclic **by construction** — the
relation engine rejects any arc that would close a blocks cycle at *create* time
(``TrackerService._would_cycle``). ``dependency_path_find`` nonetheless guards
**defensively**: it bounds traversal and, on a back-edge (a legacy cycle from
pre-guard data, or a cross-family artifact), returns a structured cycle result
naming the offending node set — it never loops, never truncates, never returns a
partial unlabelled path. Fail closed, deterministic.

Pure read over the relation table (the v0.1 reuse precedent): this reuses
``_FAMILY_TYPES`` + ``_relation_arc`` from the tracker so the graph and the
create-time cycle guard can never disagree on what an arc means.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterator
from dataclasses import dataclass

from sqlmodel import Session, col, select

from kantaq_core.tracker.service import _FAMILY_TYPES, _relation_arc
from kantaq_db.models import TicketRelationship

_BLOCKS = "blocks"

# DFS node colors for the defensive cycle guard.
_WHITE = 0  # unseen (absent from the color map)
_GRAY = 1  # on the current DFS stack
_BLACK = 2  # fully explored


def blocks_adjacency(session: Session) -> dict[str, list[str]]:
    """The blocks-family adjacency ``{src: [dst, …]}`` from the relation table.

    ``src`` blocks ``dst`` (``src`` must finish before ``dst``). The lists are
    sorted + de-duplicated so a graph or path is byte-stable across replicas.
    """
    statement = select(TicketRelationship).where(
        col(TicketRelationship.type).in_(_FAMILY_TYPES[_BLOCKS])
    )
    adjacency: dict[str, list[str]] = {}
    for relationship in session.exec(statement).all():
        arc = _relation_arc(relationship.from_id, relationship.to_id, relationship.type)
        if arc is None:  # pragma: no cover - the query restricts to arc types
            continue
        _, src, dst = arc
        adjacency.setdefault(src, []).append(dst)
    return {src: sorted(set(dsts)) for src, dsts in adjacency.items()}


@dataclass(frozen=True)
class DependencyGraph:
    """A blocks sub-graph: ``nodes`` (ticket ids) + directed ``edges`` (src blocks dst)."""

    nodes: tuple[str, ...]
    edges: tuple[tuple[str, str], ...]


def dependency_graph_get(
    session: Session,
    *,
    root_ticket_id: str | None = None,
    depth: int | None = None,
) -> DependencyGraph:
    """The blocks sub-graph as nodes + edges.

    With ``root_ticket_id`` it is the sub-graph reachable from the root via
    ``blocks`` edges (BFS), bounded by ``depth`` (``None`` = unbounded). Without a
    root it is the whole blocks graph. Deterministic (sorted) output.
    """
    adjacency = blocks_adjacency(session)
    if root_ticket_id is None:
        nodes = set(adjacency) | {dst for dsts in adjacency.values() for dst in dsts}
        edges = {(src, dst) for src, dsts in adjacency.items() for dst in dsts}
        return DependencyGraph(tuple(sorted(nodes)), tuple(sorted(edges)))

    seen = {root_ticket_id}
    edges_set: set[tuple[str, str]] = set()
    frontier: deque[tuple[str, int]] = deque([(root_ticket_id, 0)])
    while frontier:
        node, dist = frontier.popleft()
        if depth is not None and dist >= depth:
            continue
        for dst in adjacency.get(node, ()):
            edges_set.add((node, dst))
            if dst not in seen:
                seen.add(dst)
                frontier.append((dst, dist + 1))
    return DependencyGraph(tuple(sorted(seen)), tuple(sorted(edges_set)))


@dataclass(frozen=True)
class PathResult:
    """The outcome of a blocking-path query.

    ``found`` + ``path`` give the blocks chain from→to. ``cycle`` is set (and
    ``found`` is False) only when the defensive guard hit a cycle reachable from
    the source — the offending node set, so the caller can name it rather than
    silently return a wrong or partial path.
    """

    found: bool
    path: tuple[str, ...]
    cycle: tuple[str, ...] | None = None


def _find_cycle(adjacency: dict[str, list[str]], start: str) -> tuple[str, ...] | None:
    """A cycle reachable from ``start`` in the blocks graph, or None (iterative DFS).

    Bounds traversal with a color map; a back-edge to a GRAY node (one on the
    current stack) is a cycle — reconstructed from the parent chain so the
    returned tuple names exactly the offending nodes.
    """
    color: dict[str, int] = {start: _GRAY}
    parent: dict[str, str | None] = {start: None}
    stack: list[tuple[str, Iterator[str]]] = [(start, iter(adjacency.get(start, ())))]
    while stack:
        node, neighbours = stack[-1]
        nxt = next(neighbours, None)
        if nxt is None:
            color[node] = _BLACK
            stack.pop()
            continue
        state = color.get(nxt, _WHITE)
        if state == _GRAY:
            # Back-edge → cycle: walk parents from node up to the re-entered node.
            cycle = [node]
            cursor: str | None = node
            while cursor != nxt and cursor is not None:
                cursor = parent[cursor]
                if cursor is not None:
                    cycle.append(cursor)
            return tuple(reversed(cycle))
        if state == _WHITE:
            color[nxt] = _GRAY
            parent[nxt] = node
            stack.append((nxt, iter(adjacency.get(nxt, ()))))
        # _BLACK: already fully explored — skip (bounded, never re-walked).
    return None


def _bfs_path(adjacency: dict[str, list[str]], src: str, dst: str) -> tuple[str, ...] | None:
    """The shortest blocks path ``src → dst`` (BFS), or None if dst is unreachable."""
    if src == dst:
        return (src,)
    parent: dict[str, str | None] = {src: None}
    queue: deque[str] = deque([src])
    while queue:
        node = queue.popleft()
        for nxt in adjacency.get(node, ()):
            if nxt in parent:
                continue
            parent[nxt] = node
            if nxt == dst:
                path: list[str] = []
                cursor: str | None = dst
                while cursor is not None:
                    path.append(cursor)
                    cursor = parent[cursor]
                return tuple(reversed(path))
            queue.append(nxt)
    return None


def dependency_path_find(session: Session, from_ticket_id: str, to_ticket_id: str) -> PathResult:
    """The blocks path ``from → to``, or a structured cycle result (D-27).

    The defensive guard runs FIRST: if any cycle is reachable from the source,
    the result is a ``cycle`` (fail closed — a wrong blocking path is worse than
    none). Otherwise the reachable sub-graph is a DAG and BFS returns the
    shortest path (or ``found=False`` when ``to`` is simply unreachable).
    """
    adjacency = blocks_adjacency(session)
    cycle = _find_cycle(adjacency, from_ticket_id)
    if cycle is not None:
        return PathResult(found=False, path=(), cycle=cycle)
    path = _bfs_path(adjacency, from_ticket_id, to_ticket_id)
    if path is None:
        return PathResult(found=False, path=())
    return PathResult(found=True, path=path)


__all__ = [
    "DependencyGraph",
    "PathResult",
    "blocks_adjacency",
    "dependency_graph_get",
    "dependency_path_find",
]
