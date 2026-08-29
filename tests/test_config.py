import pytest
from pydantic import ValidationError

from sagasmith_service.config import Settings


def test_production_rejects_development_security_defaults() -> None:
    with pytest.raises(ValidationError, match="unsafe production configuration"):
        Settings(env="production")


def test_production_accepts_explicit_secure_dependencies() -> None:
    settings = Settings(
        env="production",
        database_url="postgresql+psycopg://sagasmith:strong-password@postgres/service",
        rate_limit_backend="redis",
        secure_cookies=True,
        public_origin="https://play.example.com",
        session_secret="production-session-secret-at-least-32-characters",
        agent_api_key="internal-agent-key-that-is-at-least-32-bytes",
        auth_context_secret="production-auth-context-secret-at-least-32-bytes",
        storage_backend="s3",
        object_access_key="private-access",
        object_secret_key="private-secret-that-is-at-least-32-bytes",
    )
    assert settings.env == "production"


def test_production_rejects_documented_placeholder_secrets() -> None:
    with pytest.raises(ValidationError, match="must be replaced"):
        Settings(
            env="production",
            database_url="postgresql+psycopg://service:real-password@postgres/service",
            rate_limit_backend="redis",
            secure_cookies=True,
            public_origin="https://play.example.com",
            session_secret="replace-with-at-least-32-random-characters",
            agent_api_key="real-internal-key-that-is-at-least-32-bytes",
            storage_backend="s3",
            object_access_key="private-access",
            object_secret_key="real-private-secret-that-is-at-least-32-bytes",
        )


def test_room_turn_reservation_must_outlive_agent_timeout() -> None:
    with pytest.raises(ValidationError, match="AGENT_RESERVATION_TTL_SECONDS must exceed"):
        Settings(
            env="test",
            agent_completion_timeout_seconds=900,
            agent_reservation_ttl_seconds=900,
        )


def test_agent_delegation_ttl_is_independent_and_capped_at_worker_limit() -> None:
    settings = Settings(env="test", agent_delegation_ttl_seconds=600)
    assert settings.agent_reservation_ttl_seconds == 1200
    with pytest.raises(ValidationError):
        Settings(env="test", agent_delegation_ttl_seconds=901)
