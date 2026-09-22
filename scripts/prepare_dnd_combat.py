"""Prepare/read a disposable combat fixture using public application operations.

Executed in the D&D acceptance container. Actual settlement is performed later
by the HTTP room -> hosted Agent -> MCP chain, never by this fixture helper.
"""

import asyncio
import json
import sys

from sagasmith_dnd.character_schema import default_character_sheet
from sagasmith_dnd_runtime.application import create_runtime
from sagasmith_dnd_runtime.operations import RequestIdentity


async def main() -> None:
    campaign_id, principal_id, mode, key = sys.argv[1:]
    runtime = create_runtime()
    identity = RequestIdentity(principal_id, campaign_id)

    async def call(name, **arguments):
        return await runtime.execute(name, arguments, context=identity)

    try:
        if mode == "read":
            print(json.dumps(await call("combat_query", campaign_id=campaign_id, view="status")))
            return
        current = await call("campaign_query", view="get", payload={"campaign_id": campaign_id})
        if current["result"]["effective_game_phase"] != "lobby":
            await call("game_phase", campaign_id=campaign_id, action="set", tool_profile="lobby",
                       expected_revision=current["result"]["revision"],
                       idempotency_key=f"{key}:lobby")
        actors = []
        for index in range(2):
            sheet = default_character_sheet()
            sheet["combat"]["hp"] = {"value": 20, "max": 20, "temp": 0}
            created = await call("character_create_from", mode="direct", payload={
                "campaign_id": campaign_id, "name": f"Acceptance actor {index}", "sheet": sheet,
            }, idempotency_key=f"{key}:actor:{index}")
            actors.append(created["result"]["id"])
        current = await call("campaign_query", view="get", payload={"campaign_id": campaign_id})
        phase = await call("game_phase", campaign_id=campaign_id, action="set",
                           tool_profile="play", expected_revision=current["result"]["revision"],
                           idempotency_key=f"{key}:play")
        started = await call("combat_start", campaign_id=campaign_id, participant_ids=actors,
                             positioning_mode="grid",
                             battle_map={"width_cells": 6, "height_cells": 6},
                             participant_config=[
                                 {"actor_id": actors[0], "initiative": 20,
                                  "position": {"x": 0, "y": 0}},
                                 {"actor_id": actors[1], "initiative": 10,
                                  "position": {"x": 1, "y": 0}},
                             ], expected_revision=phase["result"]["campaign_revision"],
                             idempotency_key=f"{key}:start")
        print(json.dumps({"campaign_id": campaign_id, "actor_id": actors[0],
                          "kind": "ability", "ability": "perception", "action": "search",
                          "dc": 10, "expected_revision": started["campaign_revision"],
                          "idempotency_key": f"{key}:search"}))
    finally:
        runtime.close()


asyncio.run(main())
