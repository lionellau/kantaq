"""A ``reco.Registry`` backed by the db skill registry (E17-T5 / FR-E17-2).

The pure recommendation engine (``kantaq_core.reco``) reads a registry through
an injected port so it stays database-free. v0.1 injects the hardcoded tuple;
v0.2 (this module) injects a snapshot of the db-backed ``skill_containers`` plus
the acting member's active ``skill_mappings`` — so a container edit changes the
contract the engine recommends, and a personal/workspace skill→tool mapping
replaces the role's default tool hint in the output. A user mapping reflects in
the recommendation, which is the whole point of the v0.2 registry.

Read-only and built from a snapshot: it never writes (the registry is off the
sync surface — edits go through ``SkillRegistryService``) and holds no session,
so the engine call stays pure over the data it was handed.
"""

from __future__ import annotations

from kantaq_core import reco
from kantaq_db.models import SkillContainerRow, SkillMappingRow

# A default agent role for a db container with an empty recommended_roles list —
# the seeded rows always carry one, so this is a fail-safe, not a normal path.
_DEFAULT_ROLE = "code_agent"


def _to_container(row: SkillContainerRow) -> reco.SkillContainer:
    """Project a db container row onto the engine's frozen contract. ``id`` is the
    slug (the engine keys on slugs, as the lifecycle/signal rules emit slugs);
    ``recommended_roles[0]`` seeds the single role v0.1 used (E17-T4 widened it to
    plural, one element per the migrated container)."""
    role = row.recommended_roles[0] if row.recommended_roles else _DEFAULT_ROLE
    return reco.SkillContainer(
        id=row.slug,
        name=row.name,
        recommended_role=role,
        supported_stages=tuple(row.supported_stages),
        expected_output=row.expected_output,
        default_write_mode=row.default_write_mode,
        risk_level=row.risk_level,
    )


def _mapping_label(mapping: SkillMappingRow) -> str:
    """The descriptive tool label a mapping surfaces (DEBT-06: never executable)."""
    return mapping.connection or mapping.provider or ""


class DbRegistry:
    """A ``reco.Registry`` over a containers + mappings snapshot."""

    def __init__(
        self, containers: list[SkillContainerRow], mappings: list[SkillMappingRow]
    ) -> None:
        self._by_slug: dict[str, reco.SkillContainer] = {
            row.slug: _to_container(row) for row in containers
        }
        slug_by_row_id = {row.id: row.slug for row in containers}
        # The active tool label per container slug. A personal mapping wins over a
        # workspace one (the member's own choice is more specific); disabled
        # mappings and label-less ones are ignored (the role hint stands).
        chosen: dict[str, tuple[str, str]] = {}
        for mapping in mappings:
            if mapping.status != "active":
                continue
            slug = slug_by_row_id.get(mapping.container_id)
            label = _mapping_label(mapping)
            if slug is None or not label:
                continue
            current = chosen.get(slug)
            if current is None or (mapping.scope == "personal" and current[0] != "personal"):
                chosen[slug] = (mapping.scope, label)
        self._tool_by_slug: dict[str, str] = {slug: label for slug, (_, label) in chosen.items()}

    def container(self, slug: str) -> reco.SkillContainer | None:
        return self._by_slug.get(slug)

    def mapped_tool(self, container: reco.SkillContainer) -> str:
        # container.id is the slug; surface the user's mapped tool if one is set.
        return self._tool_by_slug.get(container.id) or container.mapped_tool
