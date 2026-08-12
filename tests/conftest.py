from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from sagasmith_service.config import Settings
from sagasmith_service.database import make_engine
from sagasmith_service.integrations.agent import AgentResult
from sagasmith_service.main import create_app


class FakeDndRuntime:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.fail_grant = False
        self.campaign_count = 0
        self.module_revision = 1
        self.final_pack_id = ""
        self.final_pack_version = ""

    async def get_campaign(self, **arguments: Any) -> dict[str, Any]:
        self.calls.append(("campaign_get", arguments))
        return {
            "action": "get",
            "result": {
                "id": arguments["campaign_id"],
                "revision": 7,
                "effective_game_phase": "play",
            },
        }

    async def create_campaign(self, **arguments: Any) -> dict[str, Any]:
        self.calls.append(("campaign_create", arguments))
        self.campaign_count += 1
        return {"id": f"campaign-{self.campaign_count}", "revision": 1}

    async def grant_campaign_access(self, **arguments: Any) -> dict[str, Any]:
        self.calls.append(("campaign_access", arguments))
        if self.fail_grant:
            raise RuntimeError("runtime unavailable")
        return {"scope": "campaign", "result": {"role": arguments["role"]}}

    async def grant_actor_access(self, **arguments: Any) -> dict[str, Any]:
        self.calls.append(("actor_access", arguments))
        return {"scope": "actor", "result": {"actor_id": arguments["actor_id"]}}

    async def revoke_campaign_access(self, **arguments: Any) -> dict[str, Any]:
        self.calls.append(("campaign_access_revoke", arguments))
        return {
            "scope": "campaign",
            "result": {
                "campaign_id": arguments["campaign_id"],
                "principal_id": arguments["principal_id"],
                "previous_role": "player",
                "revoked_actor_grants": 1,
                "revoked": True,
            },
        }

    async def import_content_pack(self, **arguments: Any) -> dict[str, Any]:
        self.calls.append(("content_pack_import", arguments))
        return {"action": "import", "result": {"module_id": "module-1"}}

    async def import_content_artifact(self, **arguments: Any) -> dict[str, Any]:
        self.calls.append(("content_pack_import", arguments))
        return {"action": "import", "result": {"module_id": "module-1"}}

    async def activate_content_pack(self, **arguments: Any) -> dict[str, Any]:
        self.calls.append(("content_pack_activate", arguments))
        return {"action": "activate", "result": {"active": True}}

    async def get_content_artifact(self, **arguments: Any) -> dict[str, Any]:
        self.calls.append(("content_pack_get", arguments))
        return {
            "action": "get",
            "result": {
                "id": self.final_pack_id,
                "version": self.final_pack_version,
                "checksum": "a" * 64,
            },
        }

    async def module_draft(self, **arguments: Any) -> dict[str, Any]:
        self.calls.append(("module_draft", arguments))
        action = arguments["action"]
        if action == "evidence":
            return {
                "action": "evidence",
                "result": [
                    {
                        "id": "chunk-1",
                        "content": "A complete encounter with a legal ending.",
                        "source_ref": {"chunk_hash": "b" * 64},
                    }
                ],
            }
        if action == "get":
            return {
                "action": "get",
                "result": {
                    "job": {
                        "id": "draft-job-1",
                        "state": "imported",
                        "revision": self.module_revision,
                    },
                    "pack_draft": {},
                },
            }
        if action == "finalize":
            self.module_revision += 1
            self.final_pack_id = str(arguments["payload"]["pack_id"])
            self.final_pack_version = str(arguments["payload"]["version"])
            return {
                "action": "finalize",
                "result": {
                    "job": {
                        "id": "draft-job-1",
                        "state": "compiled",
                        "revision": self.module_revision,
                        "module_id": "module-authoring-1",
                    },
                    "artifact": "artifact://module/compiled-1",
                    "summary": {"pack_id": "module-pack-1", "checksum": "a" * 64},
                },
            }
        self.module_revision += 1
        return {
            "action": action,
            "result": {
                "job_id": "draft-job-1",
                "job": {
                    "id": "draft-job-1",
                    "state": "imported",
                    "revision": self.module_revision,
                    "module_id": "module-authoring-1",
                },
                "module_id": "module-authoring-1",
                "inspection": {"scenes": 3},
                "validation": {"valid": True},
            },
        }


class FakeAgentRuntime:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.fail = False
        self.content = "你进入了烛堡。"

    async def complete(self, **arguments: Any) -> AgentResult:
        self.calls.append(arguments)
        if self.fail:
            raise RuntimeError("agent unavailable")
        return AgentResult(
            content=self.content,
            request_id="agent-request-1",
            model="test-model",
            prompt_tokens=120,
            completion_tokens=30,
        )


@pytest.fixture
def dnd_runtime() -> FakeDndRuntime:
    return FakeDndRuntime()


@pytest.fixture
def agent_runtime() -> FakeAgentRuntime:
    return FakeAgentRuntime()


@pytest.fixture
def client(
    dnd_runtime: FakeDndRuntime, agent_runtime: FakeAgentRuntime, tmp_path: Path
) -> Iterator[TestClient]:
    settings = Settings(
        env="test",
        database_url="sqlite://",
        session_secret="test-session-secret-at-least-thirty-two-characters",
        private_storage_dir=str(tmp_path / "private"),
        exchange_dir=str(tmp_path / "exchange"),
        public_origin="http://testserver",
    )
    app = create_app(settings, make_engine("sqlite://"), dnd_runtime, agent_runtime)
    with TestClient(app) as value:
        value.headers["Origin"] = "http://testserver"
        yield value
