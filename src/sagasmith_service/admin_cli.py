"""Offline administration commands for the hosted beta.

The command reads passwords interactively and writes directly to the configured
database. It deliberately has no HTTP path and records each account or invite
operation as an audit event without storing credentials or raw invite tokens.
"""

from __future__ import annotations

import argparse
import getpass
import os
import secrets
from datetime import timedelta

from sqlalchemy import select, update

from sagasmith_service.database import make_engine, make_session_factory
from sagasmith_service.invite_models import RegistrationInvite
from sagasmith_service.models import AuditEvent, User, UserSession, now_utc
from sagasmith_service.security import hash_password, normalize_email, token_hash


def _password(prompt: str = "Password: ") -> str:
    value = getpass.getpass(prompt)
    if not value:
        raise ValueError("password cannot be empty")
    return value


def create_admin(database_url: str, email: str, display_name: str, password: str) -> str:
    normalized = normalize_email(email)
    with make_session_factory(make_engine(database_url)).begin() as session:
        if session.scalar(select(User.id).where(User.email == normalized)) is not None:
            raise ValueError("email is already registered")
        user = User(
            email=normalized,
            password_hash=hash_password(password),
            display_name=display_name.strip(),
            is_admin=True,
        )
        session.add(user)
        session.flush()
        session.add(
            AuditEvent(
                action="account.admin_create",
                subject_type="user",
                subject_id=user.id,
                details={"source": "offline-admin-cli", "email": normalized},
            )
        )
        return user.id


def reset_password(database_url: str, email: str, password: str) -> str:
    normalized = normalize_email(email)
    with make_session_factory(make_engine(database_url)).begin() as session:
        user = session.scalar(select(User).where(User.email == normalized))
        if user is None:
            raise ValueError("user not found")
        user.password_hash = hash_password(password)
        session.execute(
            update(UserSession)
            .where(UserSession.user_id == user.id, UserSession.revoked_at.is_(None))
            .values(revoked_at=now_utc())
        )
        session.add(
            AuditEvent(
                action="account.admin_password_reset",
                subject_type="user",
                subject_id=user.id,
                details={"source": "offline-admin-cli", "email": normalized},
            )
        )
        return user.id


def create_registration_invite(
    database_url: str,
    email: str | None,
    expires_in_hours: int,
    max_uses: int,
) -> str:
    if expires_in_hours < 1:
        raise ValueError("expires_in_hours must be positive")
    if max_uses < 1:
        raise ValueError("max_uses must be positive")
    raw_token = secrets.token_urlsafe(32)
    normalized = normalize_email(email) if email else None
    with make_session_factory(make_engine(database_url)).begin() as session:
        invite = RegistrationInvite(
            token_hash=token_hash(raw_token),
            email=normalized,
            expires_at=now_utc() + timedelta(hours=expires_in_hours),
            max_uses=max_uses,
        )
        session.add(invite)
        session.flush()
        session.add(
            AuditEvent(
                action="registration_invite.create",
                subject_type="registration_invite",
                subject_id=invite.id,
                details={
                    "source": "offline-admin-cli",
                    "email_bound": normalized is not None,
                    "expires_in_hours": expires_in_hours,
                    "max_uses": max_uses,
                },
            )
        )
    return raw_token


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Offline SagaSmith beta administration")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("SAGASMITH_DATABASE_URL", "sqlite:///./sagasmith-service.db"),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    create = commands.add_parser("create-admin", help="create an administrator account")
    create.add_argument("--email", required=True)
    create.add_argument("--display-name", required=True)

    reset = commands.add_parser("reset-password", help="reset a user's password")
    reset.add_argument("--email", required=True)

    invite = commands.add_parser("create-invite", help="create a beta registration invite")
    invite.add_argument("--email")
    invite.add_argument("--expires-in-hours", type=int, default=72)
    invite.add_argument("--max-uses", type=int, default=1)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    if args.command == "create-admin":
        user_id = create_admin(
            args.database_url,
            args.email,
            args.display_name,
            _password("Administrator password: "),
        )
        print(f"created administrator {user_id}")
    elif args.command == "reset-password":
        user_id = reset_password(args.database_url, args.email, _password())
        print(f"reset password for {user_id}")
    else:
        token = create_registration_invite(
            args.database_url, args.email, args.expires_in_hours, args.max_uses
        )
        print(token)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
