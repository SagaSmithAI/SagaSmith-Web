"""Host-side signing for SagaSmith MCP auth-context metadata."""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from datetime import UTC, datetime
from typing import Any

AUTH_CONTEXT_META_KEY = "sagasmith_auth_context"
AUTH_CONTEXT_RECEIPT_META_KEY = "sagasmith_auth_context_receipt"
AUTH_CONTEXT_SCHEMA = "sagasmith.auth-context/v1"


def sign_auth_context(
    *,
    secret: str,
    actor_principal: str,
    conversation_principal: str,
    session_id: str,
    campaign_id: str = "",
    authorization_epoch: int = 0,
) -> dict[str, Any]:
    secret_bytes = secret.encode("utf-8")
    if len(secret_bytes) < 32:
        raise ValueError("auth context secret must contain at least 32 bytes")
    payload = {
        "schema": AUTH_CONTEXT_SCHEMA,
        "host": "sagasmith-service",
        "channel": "service",
        "actor_principal": actor_principal,
        "conversation_principal": conversation_principal,
        "tenant_id": "",
        "campaign_id": campaign_id,
        "session_id": session_id,
        "principal_source": "trusted-host",
        "authorization_epoch": authorization_epoch,
        "issued_at": datetime.now(UTC).isoformat(),
        "nonce": secrets.token_urlsafe(24),
    }
    canonical = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    signature = hmac.new(secret_bytes, canonical.encode("utf-8"), hashlib.sha256).hexdigest()
    return {**payload, "signature": signature}


def exposure_revision(payload: Any, fallback: int) -> int:
    """Read a server-owned exposure revision without trusting unrelated nested revisions."""

    if not isinstance(payload, dict):
        return fallback
    current = payload.get("result", payload)
    if isinstance(current, dict):
        revision = current.get("revision")
        if isinstance(revision, int) and not isinstance(revision, bool) and revision >= 0:
            return revision
    return fallback
