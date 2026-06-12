"""Structured protocol reject errors (FR-E03-5).

Every rejection a client can act on carries a stable machine-readable
``code`` — the strings here are wire vocabulary (PRD §13.9 structured
rejects), not prose. The sync backend (Sprint 4, E24-T5) returns these codes
verbatim; prose in ``message`` may change, codes may not.
"""

from __future__ import annotations


class ProtocolError(Exception):
    """Base for all protocol rejects; ``code`` is the wire identifier."""

    code: str = "protocol_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class StaleBaseRev(ProtocolError):
    """The event's ``base_rev`` is older than the entity's committed state."""

    code = "stale_base_rev"


class PolicyDenied(ProtocolError):
    """The grant named by ``policy_ref`` does not authorize this write."""

    code = "policy_denied"


class SchemaViolation(ProtocolError):
    """The payload does not fit the canonical-encoding or entity schema."""

    code = "schema_violation"


class UnknownCollection(ProtocolError):
    """The event names a collection the receiver does not declare."""

    code = "unknown_collection"
