"""Provision the disposable container acceptance fixture offline."""

from __future__ import annotations

import os
from datetime import timedelta
from decimal import Decimal

import boto3
from botocore.exceptions import ClientError
from sqlalchemy import select

from sagasmith_service.database import make_engine, make_session_factory
from sagasmith_service.models import QuotaGrant, User, now_utc
from sagasmith_service.security import hash_password, normalize_email

OWNER_EMAIL = normalize_email("owner-e2e@example.com")
OWNER_PASSWORD = "correct horse battery staple"
QUOTA = Decimal("1000000")


def ensure_bucket() -> None:
    client = boto3.client(
        "s3",
        endpoint_url=os.environ["SAGASMITH_OBJECT_ENDPOINT"],
        region_name=os.environ.get("SAGASMITH_OBJECT_REGION", "us-east-1"),
        aws_access_key_id=os.environ["SAGASMITH_OBJECT_ACCESS_KEY"],
        aws_secret_access_key=os.environ["SAGASMITH_OBJECT_SECRET_KEY"],
    )
    bucket = os.environ["SAGASMITH_OBJECT_BUCKET"]
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        if code not in {"404", "NoSuchBucket", "NotFound"}:
            raise
        client.create_bucket(Bucket=bucket)


def require_disposable_test_environment() -> None:
    expected = {
        "SAGASMITH_ENV": "test",
        "SAGASMITH_E2E_FIXTURE": "1",
        "SAGASMITH_DATABASE_URL": (
            "postgresql+psycopg://sagasmith:e2e-postgres@postgres:5432/sagasmith_service"
        ),
        "SAGASMITH_OBJECT_ENDPOINT": "http://minio:9000",
        "SAGASMITH_OBJECT_BUCKET": "sagasmith-private-e2e",
        "SAGASMITH_OBJECT_ACCESS_KEY": "sagasmith",
        "SAGASMITH_OBJECT_SECRET_KEY": "e2e-minio-secret",
    }
    mismatched = [key for key, value in expected.items() if os.environ.get(key) != value]
    if mismatched:
        raise RuntimeError(
            "refusing to run the container E2E fixture outside its disposable test stack: "
            + ", ".join(mismatched)
        )


def seed_owner_and_quota() -> None:
    database_url = os.environ["SAGASMITH_DATABASE_URL"]
    with make_session_factory(make_engine(database_url)).begin() as session:
        owner = session.scalar(select(User).where(User.email == OWNER_EMAIL))
        if owner is None:
            owner = User(
                email=OWNER_EMAIL,
                password_hash=hash_password(OWNER_PASSWORD),
                display_name="E2E Owner",
                is_admin=True,
                terms_accepted_at=now_utc(),
                terms_version="2026-08-29",
                privacy_version="2026-08-29",
            )
            session.add(owner)
            session.flush()
        else:
            owner.is_admin = True
        start = now_utc()
        session.add(
            QuotaGrant(
                user_id=owner.id,
                metric="llm_tokens",
                quantity=QUOTA,
                period_start=start,
                period_end=start + timedelta(days=30),
                source="container-e2e-fixture",
            )
        )


if __name__ == "__main__":
    require_disposable_test_environment()
    ensure_bucket()
    seed_owner_and_quota()
    print("Container E2E bucket and offline owner fixture are ready")
