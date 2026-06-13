"""Signing-cutover config sanity (DEBT-15, E27 review MED-1/MED-2).

The cutover (D-15) is two local config values — ``sign_events`` (is signing on?)
and ``sign_cutover_rev`` (the revision below which unsigned history is exempt).
Both are unchecked ints/bools, and the E27 adversarial review of E24-T5 flagged
two ways a single wrong value silently weakens verified ingestion:

- a ``sign_cutover_rev`` **past the committed head** exempts every event up to
  it from verification — a future or copied-from-another-workspace value
  quietly disables the gate for everything below it;
- ``sign_events`` **off while signed events already exist locally** means this
  replica thinks it is pre-cutover after the workspace has cut over, so it would
  fold a peer's unsigned events instead of rejecting them.

``cutover_health`` is the pure check ``kantaq doctor`` surfaces. The full
DEBT-15 work (server-side reject, device/grant sync, owner-signed admission) is
v0.2; this is the cheap local guard that catches the misconfiguration now.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlmodel import Session, col, func, select

from kantaq_db import EventLog


@dataclass(frozen=True)
class CutoverHealth:
    """The cutover config check result — advisory lines + any warnings."""

    sign_events: bool
    cutover_rev: int
    committed_head: int
    signed_event_count: int
    warnings: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.warnings


def cutover_health(session: Session, *, sign_events: bool, sign_cutover_rev: int) -> CutoverHealth:
    """Check the local signing-cutover config against the event log."""
    committed_head = session.exec(select(func.max(EventLog.committed_rev))).one() or 0
    signed_count = session.exec(
        select(func.count()).select_from(EventLog).where(col(EventLog.sig).is_not(None))
    ).one()

    warnings: list[str] = []
    if sign_events and sign_cutover_rev > committed_head:
        warnings.append(
            f"sign_cutover_rev={sign_cutover_rev} is past the committed head "
            f"({committed_head}): verified ingestion would accept every event up to it "
            "UNVERIFIED — set it to the workspace's true cutover revision (0 for a fresh "
            "workspace)."
        )
    if not sign_events and signed_count > 0:
        warnings.append(
            f"sign_events is off but {signed_count} signed event(s) exist locally: this "
            "replica looks pre-cutover after the workspace cut over, so it would fold a "
            "peer's unsigned events instead of rejecting them — enable sign_events."
        )
    return CutoverHealth(
        sign_events=sign_events,
        cutover_rev=sign_cutover_rev,
        committed_head=int(committed_head),
        signed_event_count=int(signed_count),
        warnings=tuple(warnings),
    )
