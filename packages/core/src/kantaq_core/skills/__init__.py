"""Skill registry (MOD-22 v0.2 / E17-T4): the db-backed containers + mappings.

v0.1 kept the 29 skill containers hardcoded in ``kantaq_core.reco`` (the pure
recommendation engine, unchanged); v0.2 adds a db-backed registry seeded from
those containers. The registry is db-backed but OFF the sync surface, so its
CRUD writes locally + audited and is never emitted — see ``service`` for the
contrast with ``kantaq_core.memory``'s emit seam.
"""

from kantaq_core.skills.service import (
    MAPPING_SCOPES,
    MAPPING_STATUSES,
    RISK_LEVELS,
    ROLE_SLUGS,
    STAGE_SLUGS,
    WRITE_MODES,
    SkillNotFoundError,
    SkillRegistryError,
    SkillRegistryService,
    SkillValidationError,
)

__all__ = [
    "MAPPING_SCOPES",
    "MAPPING_STATUSES",
    "RISK_LEVELS",
    "ROLE_SLUGS",
    "STAGE_SLUGS",
    "WRITE_MODES",
    "SkillNotFoundError",
    "SkillRegistryError",
    "SkillRegistryService",
    "SkillValidationError",
]
