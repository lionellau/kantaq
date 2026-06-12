"""TelemetryCapture — the Domain (privacy) profile helper for MOD-25 (E28).

Wraps the opt-in flip and the captured-row readback so telemetry tests don't
hand-roll sessions. Imported per-test (never on the pytest plugin path — the
MOD-30 coverage rule), since it reaches ``kantaq_core``.
"""

from __future__ import annotations

from sqlalchemy.engine import Engine
from sqlmodel import Session, select

from kantaq_core.telemetry import TelemetryService
from kantaq_db.models import TelemetryEvent

HARNESS_ACTOR = "harness"


class TelemetryCapture:
    """Enable/disable capture on an engine and read back what was recorded."""

    def __init__(self, engine: Engine) -> None:
        self._engine = engine

    def enable(self, *, actor_id: str = HARNESS_ACTOR) -> None:
        with Session(self._engine) as session:
            TelemetryService(session).set_enabled(True, actor_id=actor_id)
            session.commit()

    def disable(self, *, actor_id: str = HARNESS_ACTOR) -> None:
        with Session(self._engine) as session:
            TelemetryService(session).set_enabled(False, actor_id=actor_id)
            session.commit()

    def events(self) -> list[TelemetryEvent]:
        with Session(self._engine) as session:
            return list(session.exec(select(TelemetryEvent)).all())

    def names(self) -> list[str]:
        return [event.name for event in self.events()]
