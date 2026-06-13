"""Devices API: the workspace's registered signing identities (E20-T2, MOD-06/MOD-12).

A read surface plus decommission, for Settings → Devices. A device row is one
local runtime's Ed25519 *verify* key — the private seed never leaves that
machine's keychain (D-01/RISK-03) — and the set of active rows is the
root-of-trust map grant verification resolves against (MOD-17). So:

- ``GET /v1/devices`` is open to any authenticated member: a verify key is
  public material, and seeing the trust roots is how a team reasons about who
  can sign. The response carries only the public key, never anything secret.
- ``POST /v1/devices/{id}/revoke`` is a credential-management action
  (``tokens.rotate``, Maintainer+): it drops the device from the root map and
  cascade-revokes the grants it issued (which would fail ``unknown_root``
  anyway once the row leaves the map — the cascade makes that explicit and
  audited). Revoking *this* runtime's own active device is refused (409): it
  would strand grant issuance until a re-key, which v0.1 has no flow for, so
  decommission targets devices from other runtimes.
"""

from __future__ import annotations

from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel
from sqlalchemy.engine import Engine
from sqlmodel import Session, col, select

from kantaq_core.identity import (
    Action,
    DeviceNotFoundError,
    VerifiedActor,
    local_device,
    revoke_device,
    revoke_grants_for_device,
)
from kantaq_db.models import Device, Member
from kantaq_runtime.auth import (
    get_engine_dep,
    keychain_for,
    require_action,
    require_actor,
)
from kantaq_runtime.config import Settings

router = APIRouter(prefix="/v1/devices", tags=["devices"])

EngineDep = Annotated[Engine, Depends(get_engine_dep)]
AnyActor = Annotated[VerifiedActor, Depends(require_actor)]
# Decommissioning a trust root is credential management — Maintainer and up
# (the same §11 permission that rotates and revokes member tokens/grants).
CredentialAdmin = Annotated[VerifiedActor, Depends(require_action(Action.tokens_rotate))]


class DeviceOut(BaseModel):
    """A device row — verify key only, never the private seed."""

    id: str
    label: str
    public_key: str
    member_id: str | None
    member_email: str | None
    created_at: datetime
    revoked_at: datetime | None
    active: bool
    is_current: bool

    @classmethod
    def from_row(cls, row: Device, *, member_email: str | None, is_current: bool) -> DeviceOut:
        return cls(
            id=row.id,
            label=row.label,
            public_key=row.public_key,
            member_id=row.member_id,
            member_email=member_email,
            created_at=row.created_at,
            revoked_at=row.revoked_at,
            active=row.revoked_at is None,
            is_current=is_current,
        )


def _current_device_id(session: Session, request: Request) -> str | None:
    """The id of the device this runtime signs with, if it is registered."""
    settings: Settings = request.app.state.settings
    device = local_device(session, keychain_for(settings))
    return device.id if device is not None else None


@router.get("", response_model=list[DeviceOut])
def list_devices(actor: AnyActor, engine: EngineDep, request: Request) -> list[DeviceOut]:
    with Session(engine) as session:
        current_id = _current_device_id(session, request)
        emails = {member.id: member.email for member in session.exec(select(Member)).all()}
        rows = session.exec(select(Device).order_by(col(Device.created_at), col(Device.id))).all()
        return [
            DeviceOut.from_row(
                row,
                member_email=emails.get(row.member_id) if row.member_id is not None else None,
                is_current=row.id == current_id,
            )
            for row in rows
        ]


@router.post("/{device_id}/revoke", response_model=DeviceOut)
def revoke_device_route(
    device_id: str, actor: CredentialAdmin, engine: EngineDep, request: Request
) -> DeviceOut:
    with Session(engine) as session:
        current_id = _current_device_id(session, request)
        existing = session.get(Device, device_id)
        if existing is None:
            raise HTTPException(status_code=404, detail=f"no such device: {device_id}")
        if device_id == current_id and existing.revoked_at is None:
            raise HTTPException(
                status_code=409,
                detail=(
                    "cannot decommission this runtime's own active device "
                    "(it would strand grant issuance until a re-key)"
                ),
            )
        try:
            row = revoke_device(session, device_id, actor_id=actor.member_id)
        except DeviceNotFoundError as exc:  # pragma: no cover - guarded above
            raise HTTPException(status_code=404, detail=str(exc)) from exc
        revoke_grants_for_device(session, device_id, actor_id=actor.member_id)
        session.commit()
        session.refresh(row)
        member_email: str | None = None
        if row.member_id is not None:
            member = session.get(Member, row.member_id)
            member_email = member.email if member is not None else None
        return DeviceOut.from_row(row, member_email=member_email, is_current=row.id == current_id)
