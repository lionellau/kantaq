"""Skill registry service (MOD-22 v0.2 / E17-T4): containers + mappings.

The one write path for the db-backed skill registry, mirroring the memory
service contract — validate first, apply the optimistic local write, write an
attributed audit row (MOD-07) — with one deliberate **omission** that is this
module's defining property:

**Registry writes are never emitted to a sink (off the sync surface).** Unlike
``kantaq_core.memory`` (which emits team rows via ``_emit_team_only``), this
service has no sink at all: ``skill_containers`` / ``skill_mappings`` are
db-backed but OFF the sync allowlist in v0.2 (architecture §6.1 "backend
registry"; see ``tests/test_sync_allowlists.py`` NEVER_SYNC). The registry is
local infrastructure like a config table — its CRUD writes locally and is
audited (existence is auditable) but produces no ``event_log`` row, so no push
path present or future can carry it off the machine. Cross-replica registry
sync is the v0.2+ slice; when it lands, the collection joins the allowlist and a
sink is threaded in here.

Field vocabularies (write mode / risk / scope / status / roles / stages) are
validated here, the single write path, and stored as portable VARCHARs (D-07).
``connection`` is DEBT-06 descriptive (a label, not an executable command); no
secret is ever handled (DEBT-07 moot). Timestamps are injectable (``now=``) so
tests drive them with FakeClock.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from sqlmodel import Session, select

from kantaq_core import audit, lifecycle, memory_policy
from kantaq_db.models import SkillContainerRow, SkillMappingRow

# The pinned v0.2 vocabularies (MOD-22 spec). Stored as VARCHARs for dialect
# parity (D-07); validated here, the one write path.
WRITE_MODES: tuple[str, ...] = ("propose", "read")
RISK_LEVELS: tuple[str, ...] = ("low", "medium", "high")
MAPPING_SCOPES: tuple[str, ...] = ("personal", "workspace")
MAPPING_STATUSES: tuple[str, ...] = ("active", "disabled")
# The four agent roles (MOD-21) and the nine lifecycle stages (MOD-20) — the
# registry's recommended_roles / supported_stages draw from these taxonomies.
ROLE_SLUGS: tuple[str, ...] = tuple(memory_policy.ROLE_SLUGS)
STAGE_SLUGS: tuple[str, ...] = tuple(lifecycle.STAGE_SLUGS)

_SLUG_MAX = 32
_NAME_MAX = 64

# Fields a container update may change. ``slug`` is the stable registry key and
# is deliberately absent (immutable, like memory's visibility).
_CONTAINER_PATCHABLE = frozenset(
    {
        "name",
        "recommended_roles",
        "supported_stages",
        "required_input",
        "expected_output",
        "allowed_tools",
        "default_write_mode",
        "risk_level",
    }
)
# Fields a mapping update may change. ``container_id`` is the stable parent
# reference and is immutable (re-point by delete + recreate).
_MAPPING_PATCHABLE = frozenset({"scope", "provider", "connection", "status"})


def _default_now() -> datetime:
    return datetime.now(UTC)


def _naive_utc(ts: datetime) -> datetime:
    """UTC wall time without tzinfo — the store's encoding."""
    if ts.tzinfo is None:
        return ts
    return ts.astimezone(UTC).replace(tzinfo=None)


class SkillRegistryError(Exception):
    """Base class for skill registry domain errors."""


class SkillValidationError(SkillRegistryError):
    """The request was understood but violates a domain rule (HTTP 422)."""


class SkillNotFoundError(SkillRegistryError):
    def __init__(self, collection: str, entity_id: str) -> None:
        super().__init__(f"no such {collection.rstrip('s').replace('_', ' ')}: {entity_id}")
        self.collection = collection
        self.entity_id = entity_id


class SkillRegistryService:
    """Skill container + mapping CRUD bound to one acting member and one session.

    Deliberately sink-less: the registry is off the sync surface (v0.2), so
    writes are local + audited, never emitted (contrast ``MemoryService``'s
    ``_emit_team_only``).
    """

    def __init__(
        self,
        session: Session,
        *,
        actor_id: str,
        source: str = "app",
        now: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._actor_id = actor_id
        self._source = source
        self._raw_now: Callable[[], datetime] = now or _default_now

    def _now(self) -> datetime:
        return _naive_utc(self._raw_now())

    # ------------------------------------------------------------- containers

    def create_container(
        self,
        *,
        slug: str,
        name: str,
        recommended_roles: list[str] | None = None,
        supported_stages: list[str] | None = None,
        required_input: str = "",
        expected_output: str = "",
        allowed_tools: list[str] | None = None,
        default_write_mode: str = "read",
        risk_level: str = "low",
    ) -> SkillContainerRow:
        fields = self._validated_container_fields(
            {
                "slug": slug,
                "name": name,
                "recommended_roles": list(recommended_roles)
                if recommended_roles is not None
                else [],
                "supported_stages": list(supported_stages) if supported_stages is not None else [],
                "required_input": required_input,
                "expected_output": expected_output,
                "allowed_tools": list(allowed_tools) if allowed_tools is not None else [],
                "default_write_mode": default_write_mode,
                "risk_level": risk_level,
            }
        )
        existing = self._session.exec(
            select(SkillContainerRow).where(SkillContainerRow.slug == fields["slug"])
        ).first()
        if existing is not None:
            raise SkillValidationError(f"a skill container with slug {fields['slug']!r} exists")

        ts = self._now()
        container = SkillContainerRow(created_at=ts, updated_at=ts, **fields)
        self._session.add(container)
        self._session.flush()
        self._audit("skill_container.create", "skill_containers", container, before=None, now=ts)
        self._session.commit()
        self._session.refresh(container)
        return container

    def get_container(self, container_id: str) -> SkillContainerRow:
        container = self._session.get(SkillContainerRow, container_id)
        if container is None:
            raise SkillNotFoundError("skill_containers", container_id)
        return container

    def list_containers(self) -> list[SkillContainerRow]:
        """Every container, by slug (a stable, human-readable order)."""
        rows = self._session.exec(select(SkillContainerRow)).all()
        return sorted(rows, key=lambda r: r.slug)

    def update_container(self, container_id: str, changes: dict[str, Any]) -> SkillContainerRow:
        container = self.get_container(container_id)
        if "slug" in changes:
            # The slug is the stable registry key: mutating it would strand
            # every reference (like memory's immutable visibility).
            raise SkillValidationError("slug is immutable; it is the stable registry key")
        unknown = set(changes) - _CONTAINER_PATCHABLE
        if unknown:
            raise SkillValidationError(f"unknown skill container fields: {sorted(unknown)}")
        validated = self._validated_container_fields(changes)

        before = audit.snapshot(container)
        ts = self._now()
        for fieldname, value in validated.items():
            setattr(container, fieldname, value)
        container.updated_at = ts
        self._session.add(container)
        self._session.flush()
        self._audit("skill_container.update", "skill_containers", container, before=before, now=ts)
        self._session.commit()
        self._session.refresh(container)
        return container

    def delete_container(self, container_id: str) -> None:
        container = self.get_container(container_id)
        # The FK has no ON DELETE cascade by design: deleting a container out
        # from under its mappings would strand them. Refuse with a clean domain
        # error (not a raw IntegrityError) so the caller deletes the mappings
        # first — registry containers are the taxonomy and rarely removed.
        child = self._session.exec(
            select(SkillMappingRow).where(SkillMappingRow.container_id == container_id)
        ).first()
        if child is not None:
            raise SkillValidationError(
                f"skill container {container_id} still has mappings; delete them first"
            )
        before = audit.snapshot(container)
        ts = self._now()
        self._audit(
            "skill_container.delete",
            "skill_containers",
            container,
            before=before,
            after_none=True,
            now=ts,
        )
        self._session.delete(container)
        self._session.commit()

    # -------------------------------------------------------------- mappings

    def create_mapping(
        self,
        *,
        container_id: str,
        scope: str = "personal",
        provider: str = "",
        connection: str = "",
        status: str = "active",
    ) -> SkillMappingRow:
        # Validate the FK explicitly for a clean 422 (not a raw IntegrityError).
        if self._session.get(SkillContainerRow, container_id) is None:
            raise SkillValidationError(f"no such skill container: {container_id}")
        fields = self._validated_mapping_fields(
            {
                "scope": scope,
                "provider": provider,
                "connection": connection,
                "status": status,
            }
        )

        ts = self._now()
        mapping = SkillMappingRow(
            container_id=container_id,
            created_by=self._actor_id,
            created_at=ts,
            updated_at=ts,
            **fields,
        )
        self._session.add(mapping)
        self._session.flush()
        self._audit("skill_mapping.create", "skill_mappings", mapping, before=None, now=ts)
        self._session.commit()
        self._session.refresh(mapping)
        return mapping

    def get_mapping(self, mapping_id: str) -> SkillMappingRow:
        mapping = self._session.get(SkillMappingRow, mapping_id)
        if mapping is None:
            raise SkillNotFoundError("skill_mappings", mapping_id)
        return mapping

    def list_mappings(
        self, *, container_id: str | None = None, scope: str | None = None
    ) -> list[SkillMappingRow]:
        statement = select(SkillMappingRow)
        if container_id is not None:
            statement = statement.where(SkillMappingRow.container_id == container_id)
        if scope is not None:
            statement = statement.where(SkillMappingRow.scope == scope)
        rows = self._session.exec(statement).all()
        return sorted(rows, key=lambda r: r.id)

    def update_mapping(self, mapping_id: str, changes: dict[str, Any]) -> SkillMappingRow:
        mapping = self.get_mapping(mapping_id)
        if "container_id" in changes:
            # The parent reference is the stable key: re-point by delete + create.
            raise SkillValidationError("container_id is immutable; recreate to re-point a mapping")
        unknown = set(changes) - _MAPPING_PATCHABLE
        if unknown:
            raise SkillValidationError(f"unknown skill mapping fields: {sorted(unknown)}")
        validated = self._validated_mapping_fields(changes)

        before = audit.snapshot(mapping)
        ts = self._now()
        for fieldname, value in validated.items():
            setattr(mapping, fieldname, value)
        mapping.updated_at = ts
        self._session.add(mapping)
        self._session.flush()
        self._audit("skill_mapping.update", "skill_mappings", mapping, before=before, now=ts)
        self._session.commit()
        self._session.refresh(mapping)
        return mapping

    def delete_mapping(self, mapping_id: str) -> None:
        mapping = self.get_mapping(mapping_id)
        before = audit.snapshot(mapping)
        ts = self._now()
        self._audit(
            "skill_mapping.delete",
            "skill_mappings",
            mapping,
            before=before,
            after_none=True,
            now=ts,
        )
        self._session.delete(mapping)
        self._session.commit()

    # --------------------------------------------------------------- helpers

    def _audit(
        self,
        action: str,
        collection: str,
        row: SkillContainerRow | SkillMappingRow,
        *,
        before: dict[str, Any] | None,
        after_none: bool = False,
        now: datetime,
    ) -> None:
        """One attributed audit row per write (MOD-07).

        Registry rows carry no privacy boundary (they are non-sensitive
        reference data), so snapshots are always recorded — but the row never
        reaches the sync surface, so the trail stays local like the registry.
        """
        after = None if after_none else audit.snapshot(row)
        audit.write(
            self._session,
            actor_id=self._actor_id,
            action=action,
            source=self._source,
            object_ref=f"{collection}/{row.id}",
            before=before,
            after=after,
            now=now,
        )

    def _validated_container_fields(self, fields: dict[str, Any]) -> dict[str, Any]:
        out = dict(fields)
        if "slug" in out:
            out["slug"] = str(out["slug"]).strip()
            if not out["slug"]:
                raise SkillValidationError("a skill container needs a non-empty slug")
            if len(out["slug"]) > _SLUG_MAX:
                raise SkillValidationError(f"slug exceeds {_SLUG_MAX} characters")
        if "name" in out:
            out["name"] = str(out["name"]).strip()
            if not out["name"]:
                raise SkillValidationError("a skill container needs a non-empty name")
            if len(out["name"]) > _NAME_MAX:
                raise SkillValidationError(f"name exceeds {_NAME_MAX} characters")
        for fieldname, vocabulary in (
            ("default_write_mode", WRITE_MODES),
            ("risk_level", RISK_LEVELS),
        ):
            if fieldname in out and out[fieldname] not in vocabulary:
                raise SkillValidationError(
                    f"unknown {fieldname} {out[fieldname]!r}; expected one of {vocabulary}"
                )
        if "recommended_roles" in out:
            out["recommended_roles"] = self._validated_list(
                out["recommended_roles"], "recommended_roles", ROLE_SLUGS, "role"
            )
        if "supported_stages" in out:
            out["supported_stages"] = self._validated_list(
                out["supported_stages"], "supported_stages", STAGE_SLUGS, "stage"
            )
        if "allowed_tools" in out:
            out["allowed_tools"] = self._validated_str_list(out["allowed_tools"], "allowed_tools")
        return out

    def _validated_mapping_fields(self, fields: dict[str, Any]) -> dict[str, Any]:
        out = dict(fields)
        for fieldname, vocabulary in (
            ("scope", MAPPING_SCOPES),
            ("status", MAPPING_STATUSES),
        ):
            if fieldname in out and out[fieldname] not in vocabulary:
                raise SkillValidationError(
                    f"unknown {fieldname} {out[fieldname]!r}; expected one of {vocabulary}"
                )
        return out

    def _validated_list(
        self, value: Any, fieldname: str, vocabulary: tuple[str, ...], item_name: str
    ) -> list[str]:
        items = self._validated_str_list(value, fieldname)
        for item in items:
            if item not in vocabulary:
                raise SkillValidationError(
                    f"unknown {item_name} {item!r}; expected one of {vocabulary}"
                )
        return items

    def _validated_str_list(self, value: Any, fieldname: str) -> list[str]:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise SkillValidationError(f"{fieldname} must be a list of strings")
        return list(value)
