"""Bootstrap a self-hosted workspace (E25-T2 / MOD-28).

The maintainer-apply step for the self-hosted backend: mint the founding member
and their bearer token so a runtime can authenticate. The token is shown once
(only its Argon2id hash is stored), exactly like the Supabase-mode owner token.

    docker compose exec sync-server uv run python -m kantaq_backend_postgres.seed \
        --email you@team.dev --workspace "Acme"

prints a ``kq_…`` token; put it in the runtime's ``.env`` as ``HUB_TOKEN``.
"""

from __future__ import annotations

import argparse
import os

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine, make_url
from sqlmodel import Session, select

from kantaq_backend_postgres.schema import create_schema
from kantaq_core.identity.tokens import mint_token
from kantaq_db.models import Member, Token, Workspace


def seed_member(
    engine: Engine,
    *,
    email: str,
    workspace_name: str = "Workspace",
    role: str = "Owner",
) -> tuple[str, str]:
    """Create (or reuse) a workspace + member and mint a fresh token.

    Returns ``(member_id, token_plaintext)``. Idempotent on the workspace (reuses
    the first one if present) and the member (reuses a row with the same email);
    a new token is minted each call so this also serves as a token-rotate.
    """
    create_schema(engine)
    with Session(engine) as session:
        workspace = session.exec(select(Workspace)).first()
        if workspace is None:
            workspace = Workspace(name=workspace_name)
            session.add(workspace)
            session.flush()
        member = session.exec(select(Member).where(Member.email == email)).first()
        if member is None:
            member = Member(workspace_id=workspace.id, email=email, role=role, status="active")
            session.add(member)
            session.flush()
        token = Token(member_id=member.id, hashed="")
        session.add(token)
        session.flush()  # assign the token id
        plaintext, phc = mint_token(token.id)
        token.hashed = phc
        session.add(token)
        session.commit()
        return member.id, plaintext


def main() -> None:
    parser = argparse.ArgumentParser(description="Mint a self-hosted member token (MOD-28).")
    parser.add_argument("--email", required=True, help="the member's email")
    parser.add_argument("--workspace", default="Workspace", help="workspace name (if creating one)")
    parser.add_argument("--role", default="Owner", help="member role (default Owner)")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("KANTAQ_DATABASE_URL"),
        help="Postgres URL (defaults to KANTAQ_DATABASE_URL)",
    )
    args = parser.parse_args()
    if not args.database_url:
        raise SystemExit("KANTAQ_DATABASE_URL (or --database-url) is required")
    url = make_url(args.database_url)
    if url.drivername == "postgresql":
        url = url.set(drivername="postgresql+psycopg")
    engine = create_engine(url)
    member_id, token = seed_member(
        engine, email=args.email, workspace_name=args.workspace, role=args.role
    )
    print(f"member: {member_id}")
    print(f"token:  {token}")
    print("Put the token in the runtime's .env as HUB_TOKEN (shown once).")


if __name__ == "__main__":
    main()
