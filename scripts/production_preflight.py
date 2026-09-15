"""Fail-closed checks for the digest-pinned beta deployment inputs.

This command validates configuration shape only. It never contacts the object
store, pulls an image, or claims that a release is deployed.
"""

from __future__ import annotations

import argparse
import json
import os
import re
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from urllib.parse import urlparse

DIGEST_IMAGE = re.compile(r"^\S+@sha256:[0-9a-f]{64}$")
REQUIRED_IMAGES = (
    "SAGASMITH_WEB_IMAGE",
    "SAGASMITH_AGENT_IMAGE",
    "SAGASMITH_DND_IMAGE",
    "SAGASMITH_COC_IMAGE",
)


def load_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.is_file():
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line[7:].lstrip()
            key, separator, value = line.partition("=")
            if separator and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key.strip()):
                values[key.strip()] = value.strip().strip("'\"")
    values.update(
        {
            key: value
            for key, value in os.environ.items()
            if key.startswith("SAGASMITH_") or key in {
                "OPENAI_API_KEY", "DEEPSEEK_API_KEY", "POSTGRES_PASSWORD"
            }
        }
    )
    return values


def validate(values: dict[str, str]) -> list[str]:
    failures: list[str] = []

    def is_placeholder(value: str) -> bool:
        lowered = value.casefold()
        return any(marker in lowered for marker in ("replace-", "change-me", "development-only"))

    try:
        enabled = json.loads(values.get(
            "SAGASMITH_ENABLED_SYSTEMS", '["dnd5e","coc7e","narrative"]'
        ))
    except ValueError:
        enabled = None
    if not isinstance(enabled, list) or not enabled or not all(
        isinstance(item, str) for item in enabled
    ) or not set(enabled) <= {
        "dnd5e", "coc7e", "narrative"
    }:
        failures.append("SAGASMITH_ENABLED_SYSTEMS must list supported systems")
        enabled = ["dnd5e", "coc7e", "narrative"]
    for key in REQUIRED_IMAGES:
        if key == "SAGASMITH_COC_IMAGE" and "coc7e" not in enabled:
            continue
        value = values.get(key, "")
        if not DIGEST_IMAGE.fullmatch(value):
            failures.append(f"{key} must be a complete image@sha256:<64-hex> reference")
    if values.get("SAGASMITH_ENV") != "production":
        failures.append("SAGASMITH_ENV must be production")
    if values.get("SAGASMITH_STORAGE_BACKEND") != "s3":
        failures.append("SAGASMITH_STORAGE_BACKEND must be s3")
    if values.get("SAGASMITH_PROVIDER_BUDGET_ENABLED", "false").casefold() != "true":
        failures.append("SAGASMITH_PROVIDER_BUDGET_ENABLED must be true")
    prices_raw = values.get("SAGASMITH_PROVIDER_PRICES", "")
    if not prices_raw or is_placeholder(prices_raw):
        failures.append("SAGASMITH_PROVIDER_PRICES must contain reviewed versioned JSON prices")
    else:
        try:
            prices = json.loads(prices_raw)
        except json.JSONDecodeError:
            failures.append("SAGASMITH_PROVIDER_PRICES must be valid JSON")
        else:
            if not isinstance(prices, dict) or not prices:
                failures.append("SAGASMITH_PROVIDER_PRICES must be a non-empty object")
            else:
                for model, price in prices.items():
                    if not isinstance(model, str) or not isinstance(price, dict):
                        failures.append("SAGASMITH_PROVIDER_PRICES entries must be model objects")
                        break
                    required = {
                        "provider", "version", "input_usd_per_million",
                        "cached_usd_per_million", "output_usd_per_million",
                        "max_input_tokens", "max_output_tokens", "max_request_bytes",
                        "valid_until",
                    }
                    if not required.issubset(price):
                        failures.append(f"SAGASMITH_PROVIDER_PRICES entry {model!r} is incomplete")
                        break
                    if price.get("provider") != "OpenAICompatProvider" or not price.get("version"):
                        failures.append(
                            f"SAGASMITH_PROVIDER_PRICES entry {model!r} has no provider/version"
                        )
                        break
                    try:
                        valid_until = datetime.fromisoformat(
                            str(price["valid_until"]).replace("Z", "+00:00")
                        )
                        if (
                            valid_until.tzinfo is None
                            or valid_until.astimezone(timezone.utc) <= datetime.now(timezone.utc)
                        ):
                            raise ValueError
                    except (TypeError, ValueError):
                        failures.append(
                            f"SAGASMITH_PROVIDER_PRICES entry {model!r} has invalid or "
                            "expired valid_until"
                        )
                        break
                    try:
                        if any(
                            not Decimal(str(price[field])).is_finite()
                            or Decimal(str(price[field])) <= 0
                            for field in (
                                "input_usd_per_million",
                                "output_usd_per_million",
                                "max_input_tokens",
                                "max_output_tokens",
                                "max_request_bytes",
                            )
                        ) or not Decimal(str(price["cached_usd_per_million"])).is_finite() or (
                            Decimal(str(price["cached_usd_per_million"])) < 0
                        ):
                            raise ValueError
                    except (InvalidOperation, ValueError, TypeError):
                        failures.append(
                            f"SAGASMITH_PROVIDER_PRICES entry {model!r} has invalid limits or rates"
                        )
                        break
    task_budget = values.get("SAGASMITH_TASK_BUDGET_USD", "")
    if not task_budget or is_placeholder(task_budget):
        failures.append("SAGASMITH_TASK_BUDGET_USD must be a reviewed positive amount")
    else:
        try:
            amount = Decimal(task_budget)
            if not amount.is_finite() or amount <= 0:
                failures.append("SAGASMITH_TASK_BUDGET_USD must be positive")
        except (ValueError, InvalidOperation):
            failures.append("SAGASMITH_TASK_BUDGET_USD must be numeric")
    for key in (
        "SAGASMITH_USER_UPLOAD_STORAGE_BYTES",
        "SAGASMITH_MAX_PACK_BYTES",
        "SAGASMITH_MAX_MODULE_SOURCE_BYTES",
        "SAGASMITH_ROOM_TURN_MEDIA_MAX_BYTES",
    ):
        try:
            if int(values.get(key, "0")) <= 0:
                failures.append(f"{key} must be a positive finite limit")
        except ValueError:
            failures.append(f"{key} must be an integer limit")
    database_url = values.get("SAGASMITH_DATABASE_URL", "")
    if not database_url.startswith("postgresql+psycopg://") or is_placeholder(database_url):
        failures.append("SAGASMITH_DATABASE_URL must be a non-placeholder PostgreSQL URL")
    public_origin = values.get("SAGASMITH_PUBLIC_ORIGIN", "")
    if not public_origin.startswith("https://") or is_placeholder(public_origin):
        failures.append("SAGASMITH_PUBLIC_ORIGIN must be a non-placeholder https origin")
    if values.get("SAGASMITH_AGENT_BOUNDARY_MODE") != "modern":
        failures.append("SAGASMITH_AGENT_BOUNDARY_MODE must be modern")
    object_endpoint = values.get("SAGASMITH_OBJECT_ENDPOINT", "")
    if not object_endpoint.startswith("https://"):
        failures.append("SAGASMITH_OBJECT_ENDPOINT must use https")
    elif urlparse(object_endpoint).hostname in {"localhost", "127.0.0.1", "::1"}:
        failures.append("SAGASMITH_OBJECT_ENDPOINT must be an external endpoint")
    for key in (
        "SAGASMITH_OBJECT_REGION",
        "SAGASMITH_OBJECT_BUCKET",
        "SAGASMITH_OBJECT_ACCESS_KEY",
        "SAGASMITH_OBJECT_SECRET_KEY",
    ):
        if not values.get(key) or is_placeholder(values[key]):
            failures.append(f"{key} must be supplied")
    if values.get("SAGASMITH_OBJECT_CREATE_BUCKET", "false").casefold() != "false":
        failures.append("SAGASMITH_OBJECT_CREATE_BUCKET must be false for production")
    if values.get("SAGASMITH_REGISTRATION_MODE", "invite") not in {"invite", "closed"}:
        failures.append("SAGASMITH_REGISTRATION_MODE must be invite or closed")
    if values.get("SAGASMITH_SIGNUP_TOKEN_QUOTA", "0") != "0":
        failures.append("SAGASMITH_SIGNUP_TOKEN_QUOTA must be 0 for beta bootstrap")
    if values.get("SAGASMITH_BOOTSTRAP_ADMIN_EMAIL", ""):
        failures.append("SAGASMITH_BOOTSTRAP_ADMIN_EMAIL must be empty")
    for key in (
        "POSTGRES_PASSWORD",
        "SAGASMITH_SESSION_SECRET",
        "SAGASMITH_AGENT_API_KEY",
        "SAGASMITH_AGENT_INTERNAL_KEY",
        "SAGASMITH_WORKER_SERVICE_TOKEN",
        "SAGASMITH_AUTH_CONTEXT_SECRET",
    ):
        if not values.get(key) or is_placeholder(values[key]):
            failures.append(f"{key} must be supplied")
        elif len(values[key]) < 32:
            failures.append(f"{key} must contain at least 32 characters")
    provider_key = (
        "DEEPSEEK_API_KEY" if values.get("SAGASMITH_MODEL_PROFILE") == "dnd-flash"
        else "OPENAI_API_KEY"
    )
    if not values.get(provider_key) or is_placeholder(values[provider_key]):
        failures.append(f"{provider_key} must be supplied")
    try:
        completion_timeout = int(values.get("SAGASMITH_AGENT_COMPLETION_TIMEOUT_SECONDS", "900"))
        reservation_ttl = int(values.get("SAGASMITH_AGENT_RESERVATION_TTL_SECONDS", "1200"))
        delegation_ttl = int(values.get("SAGASMITH_AGENT_DELEGATION_TTL_SECONDS", "600"))
        if reservation_ttl <= completion_timeout:
            failures.append(
                "SAGASMITH_AGENT_RESERVATION_TTL_SECONDS must exceed completion timeout"
            )
        if delegation_ttl > 900:
            failures.append(
                "SAGASMITH_AGENT_DELEGATION_TTL_SECONDS must be at most 900"
            )
    except ValueError:
        failures.append("Agent timeout settings must be integers")
    if values.get("SAGASMITH_AGENT_MAX_WORKERS", "6") != "6":
        failures.append("SAGASMITH_AGENT_MAX_WORKERS must be 6 for the beta profile")
    if values.get("SAGASMITH_AGENT_SPAWN_CONCURRENCY", "1") != "1":
        failures.append("SAGASMITH_AGENT_SPAWN_CONCURRENCY must be 1 for the beta profile")
    if values.get("SAGASMITH_ROOM_TURN_WORKER_CONCURRENCY", "2") != "2":
        failures.append("SAGASMITH_ROOM_TURN_WORKER_CONCURRENCY must be 2 for the beta profile")
    if values.get("SAGASMITH_ROOM_TURN_PER_ROOM_CONCURRENCY", "1") != "1":
        failures.append("SAGASMITH_ROOM_TURN_PER_ROOM_CONCURRENCY must be 1 for the beta profile")
    return failures


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--env-file", type=Path, default=Path(".env.production"))
    args = parser.parse_args()
    failures = validate(load_env(args.env_file))
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}")
        return 1
    print(
        f"Production beta preflight passed for {args.env_file}; runtime connectivity "
        "and release availability remain unverified."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
