"""Hosted Narrative control-plane adapter backed by the Agent Supervisor."""

from __future__ import annotations

from typing import Any

import httpx


class HttpNarrativeRuntime:
    def __init__(self, base_url: str, api_key: str, *, timeout_seconds: int = 180) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.timeout_seconds = timeout_seconds

    @property
    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.api_key}"}

    async def probe(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(15, connect=5)) as client:
                response = await client.get(
                    f"{self.base_url}/health/narrative", headers=self._headers
                )
            response.raise_for_status()
        except Exception as exc:
            raise RuntimeError("Narrative readiness probe failed") from exc

    async def _operation(self, operation: str, arguments: dict[str, Any]) -> dict[str, Any]:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout_seconds, connect=10)
        ) as client:
            response = await client.post(
                f"{self.base_url}/v1/narrative/operations",
                headers=self._headers,
                json={"operation": operation, "arguments": arguments},
            )
        if response.status_code >= 400:
            raise RuntimeError(f"Narrative Supervisor returned HTTP {response.status_code}")
        value = response.json()
        if not isinstance(value, dict):
            raise RuntimeError("Narrative Supervisor returned an invalid result")
        return value

    async def create_campaign(
        self,
        *,
        name: str,
        description: str,
        principal_id: str,
        idempotency_key: str,
        **_ignored: Any,
    ) -> dict[str, Any]:
        return await self._operation(
            "create_campaign",
            {
                "name": name,
                "description": description,
                "principal_id": principal_id,
                "idempotency_key": idempotency_key,
            },
        )

    async def get_campaign(self, *, campaign_id: str, principal_id: str) -> dict[str, Any]:
        return await self._operation(
            "get_campaign",
            {"campaign_id": campaign_id, "principal_id": principal_id},
        )

    async def grant_campaign_access(
        self,
        *,
        campaign_id: str,
        principal_id: str,
        role: str,
        by_principal_id: str,
    ) -> dict[str, Any]:
        return await self._operation(
            "grant_campaign_access",
            {
                "campaign_id": campaign_id,
                "principal_id": by_principal_id,
                "target_principal_id": principal_id,
                "role": role,
                "idempotency_key": (
                    f"service:narrative:grant:{campaign_id}:{principal_id}:{role}"
                ),
            },
        )

    async def revoke_campaign_access(
        self,
        *,
        campaign_id: str,
        principal_id: str,
        by_principal_id: str,
    ) -> dict[str, Any]:
        return await self._operation(
            "revoke_campaign_access",
            {
                "campaign_id": campaign_id,
                "principal_id": by_principal_id,
                "target_principal_id": principal_id,
                "idempotency_key": f"service:narrative:revoke:{campaign_id}:{principal_id}",
            },
        )

    async def grant_actor_access(
        self,
        *,
        campaign_id: str,
        actor_id: str,
        principal_id: str,
        can_control: bool,
        can_view_private: bool,
        by_principal_id: str,
    ) -> dict[str, Any]:
        return await self._operation(
            "grant_actor_access",
            {
                "campaign_id": campaign_id,
                "principal_id": by_principal_id,
                "target_principal_id": principal_id,
                "actor_id": actor_id,
                "can_control": can_control,
                "can_view_private": can_view_private,
                "idempotency_key": (
                    f"service:narrative:actor:{campaign_id}:{actor_id}:{principal_id}:"
                    f"{int(can_control)}:{int(can_view_private)}"
                ),
            },
        )

    async def get_panel_state(
        self, *, campaign_id: str, principal_id: str
    ) -> dict[str, Any]:
        campaign = await self.get_campaign(
            campaign_id=campaign_id, principal_id=principal_id
        )
        return {
            "campaign": campaign,
            "phase": campaign.get("phase", "lobby"),
            "revision": int(campaign.get("revision") or 0),
            "party": {"members": []},
            "characters": [],
            "modules": [],
            "current_module": None,
            "combat": None,
        }

    async def set_game_phase(
        self,
        *,
        campaign_id: str,
        principal_id: str,
        tool_profile: str,
        expected_revision: int,
        idempotency_key: str,
    ) -> dict[str, Any]:
        return await self._operation(
            "set_game_phase",
            {
                "campaign_id": campaign_id,
                "principal_id": principal_id,
                "phase": tool_profile,
                "expected_revision": expected_revision,
                "idempotency_key": idempotency_key,
            },
        )
