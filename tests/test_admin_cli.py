from sqlalchemy import select

from sagasmith_service.admin_cli import create_admin, create_registration_invite, reset_password
from sagasmith_service.database import Base, make_engine, make_session_factory
from sagasmith_service.invite_models import RegistrationInvite
from sagasmith_service.models import AuditEvent, User
from sagasmith_service.security import token_hash, verify_password


def test_offline_admin_commands_create_reset_and_audit(tmp_path) -> None:
    database_url = f"sqlite:///{(tmp_path / 'admin.db').as_posix()}"
    engine = make_engine(database_url)
    Base.metadata.create_all(engine)

    user_id = create_admin(database_url, "Admin@Example.com", "Admin", "correct-horse-battery")
    invite_token = create_registration_invite(database_url, None, 24, 2)
    reset_password(database_url, "ADMIN@example.com", "new-correct-horse-battery")

    with make_session_factory(engine)() as session:
        user = session.get(User, user_id)
        assert user is not None and user.is_admin
        assert verify_password(user.password_hash, "new-correct-horse-battery")
        invite = session.scalar(select(RegistrationInvite))
        assert invite is not None and invite.max_uses == 2
        assert invite.token_hash == token_hash(invite_token)
        actions = set(session.scalars(select(AuditEvent.action)))
        assert {
            "account.admin_create",
            "account.admin_password_reset",
            "registration_invite.create",
        } <= actions
    engine.dispose()
