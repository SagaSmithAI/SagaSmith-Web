from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parents[1]))

from scripts.production_preflight import validate  # noqa: E402


def _valid_values() -> dict[str, str]:
    digest = "a" * 64
    return {
        "SAGASMITH_ENV": "production",
        "SAGASMITH_PUBLIC_ORIGIN": "https://beta.example",
        "SAGASMITH_DATABASE_URL": "postgresql+psycopg://sagasmith:password@postgres:5432/sagasmith_service",
        "SAGASMITH_STORAGE_BACKEND": "s3",
        "SAGASMITH_PROVIDER_BUDGET_ENABLED": "true",
        "SAGASMITH_PROVIDER_PRICES": json.dumps(
            {
                "test-model": {
                    "provider": "OpenAICompatProvider",
                    "version": "reviewed-test",
                    "valid_until": "2099-01-01T00:00:00Z",
                    "input_usd_per_million": "1",
                    "cached_usd_per_million": "0.5",
                    "output_usd_per_million": "2",
                    "max_input_tokens": 65536,
                    "max_output_tokens": 4096,
                    "max_request_bytes": 1048576,
                }
            }
        ),
        "SAGASMITH_TASK_BUDGET_USD": "1",
        "SAGASMITH_USER_UPLOAD_STORAGE_BYTES": "536870912",
        "SAGASMITH_MAX_PACK_BYTES": "67108864",
        "SAGASMITH_MAX_MODULE_SOURCE_BYTES": "33554432",
        "SAGASMITH_ROOM_TURN_MEDIA_MAX_BYTES": "8388608",
        "SAGASMITH_OBJECT_ENDPOINT": "https://sgp1.digitaloceanspaces.com",
        "SAGASMITH_OBJECT_REGION": "sgp1",
        "SAGASMITH_OBJECT_BUCKET": "private-beta",
        "SAGASMITH_OBJECT_ACCESS_KEY": "spaces-key",
        "SAGASMITH_OBJECT_SECRET_KEY": "s" * 32,
        "SAGASMITH_OBJECT_CREATE_BUCKET": "false",
        "SAGASMITH_REGISTRATION_MODE": "invite",
        "SAGASMITH_SIGNUP_TOKEN_QUOTA": "0",
        "SAGASMITH_AGENT_BOUNDARY_MODE": "modern",
        "POSTGRES_PASSWORD": "p" * 32,
        "SAGASMITH_SESSION_SECRET": "s" * 32,
        "SAGASMITH_AGENT_API_KEY": "a" * 32,
        "SAGASMITH_AGENT_INTERNAL_KEY": "i" * 32,
        "SAGASMITH_WORKER_SERVICE_TOKEN": "w" * 32,
        "SAGASMITH_AUTH_CONTEXT_SECRET": "c" * 32,
        "OPENAI_API_KEY": "o" * 32,
        "SAGASMITH_AGENT_MAX_WORKERS": "6",
        "SAGASMITH_AGENT_SPAWN_CONCURRENCY": "1",
        "SAGASMITH_ROOM_TURN_PER_ROOM_CONCURRENCY": "1",
        "SAGASMITH_WEB_IMAGE": f"ghcr.io/example/web@sha256:{digest}",
        "SAGASMITH_AGENT_IMAGE": f"ghcr.io/example/agent@sha256:{digest}",
        "SAGASMITH_DND_IMAGE": f"ghcr.io/example/dnd@sha256:{digest}",
        "SAGASMITH_COC_IMAGE": f"ghcr.io/example/coc@sha256:{digest}",
    }


def test_production_preflight_accepts_digest_pinned_s3_beta() -> None:
    assert validate(_valid_values()) == []


def test_dnd_flash_preflight_needs_only_selected_provider_and_domain() -> None:
    values = _valid_values()
    values.update(SAGASMITH_ENABLED_SYSTEMS='["dnd5e"]',
                  SAGASMITH_MODEL_PROFILE="dnd-flash", DEEPSEEK_API_KEY="test-provider-key")
    del values["OPENAI_API_KEY"]
    del values["SAGASMITH_COC_IMAGE"]
    assert validate(values) == []
    values["DEEPSEEK_API_KEY"] = ""
    assert "DEEPSEEK_API_KEY must be supplied" in validate(values)


def test_production_preflight_rejects_tags_and_bucket_creation() -> None:
    values = _valid_values()
    values["SAGASMITH_WEB_IMAGE"] = "ghcr.io/example/web:latest"
    values["SAGASMITH_OBJECT_CREATE_BUCKET"] = "true"
    failures = validate(values)
    assert any("SAGASMITH_WEB_IMAGE" in failure for failure in failures)
    assert any("SAGASMITH_OBJECT_CREATE_BUCKET" in failure for failure in failures)


def test_hosted_agent_example_preserves_all_dynamic_mcp_servers() -> None:
    config = json.loads(Path("config/agent-config.example.json").read_text(encoding="utf-8"))
    hosted = config["modelPresets"]["hosted"]
    assert hosted["maxTokens"] == 4096
    assert hosted["contextWindowTokens"] == 65536
    assert config["agents"]["defaults"]["maxToolIterations"] == 12
    assert "module" in config["modelPresets"]
    assert set(config["tools"]["mcpServers"]) == {
        "sagasmith-dnd-mcp",
        "sagasmith-coc-mcp",
        "sagasmith-narrative-mcp",
    }
