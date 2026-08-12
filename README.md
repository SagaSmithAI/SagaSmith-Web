# SagaSmith Service

Private hosted SagaSmith product. The public SagaSmith repositories remain complete local and
self-hosted applications; this repository adds the hosted account, quota, campaign-lobby,
orchestration, unified Web, billing, and operations layers without becoming an authority for D&D
game state.

The same deployment includes **SagaSmith Forge**, the account-scoped creation and sharing layer for
Rule/Module Packs, character blueprints, Souls, Skills, assets, and hosted DM/Keeper Identities.
Forge shares catalog, immutable release, license, provenance, discussion, favorite, report, and
moderation primitives while keeping each artifact type's installation semantics explicit.

## Current target

The first production slice proves the complete trust chain:

1. two users register and sign in;
2. one creates a D&D campaign and becomes owner/DM in the authoritative MCP;
3. the second applies and is approved as a player;
4. a PC is bound to that player through MCP authority;
5. an Agent request reserves quota, records actual usage, and releases the remainder;
6. revocation immediately removes the player's next legal native MCP call;
7. local authoring remains draft/review/finalize, while the hosted library privately uploads,
   imports, and activates only the resulting immutable Pack.
8. original artifacts move through draft -> real hosted Agent review -> administrator moderation ->
   immutable publication; public Packs install through the same authoritative MCP facade.
9. a hosted DM Identity accepts a campaign invitation, receives an `agent:<uuid>` MCP grant, uses a
   pinned Soul release and campaign-isolated revisioned memory, and loses access on revocation.
10. Module Studio runs the complete D&D authoring product flow: brief/source -> outline approval ->
    persistent Agent generation -> MCP-owned evidence review/edit -> explicit finalization ->
    immutable release -> cross-campaign import and activation. Pack is an internal compiled artifact,
    not a user-facing authoring concept.

## Local development

```powershell
Copy-Item .env.example .env
docker compose up --build
```

API health through Caddy: `http://127.0.0.1/api/health`

Run tests directly:

```powershell
uv sync --all-extras
uv run pytest
uv run ruff check .
```

The real container acceptance suite is intentionally separate from unit tests. It boots
PostgreSQL, Redis, MinIO, the public D&D MCP server, real Nanobot workers, the persistent Module
Studio worker and a deterministic
OpenAI-compatible test provider, then verifies account/lobby, authenticated Agent identity,
dynamic native-tool refresh and execution, exact quota settlement, finalized Pack upload/import/
activation, membership revocation and audit:

```powershell
docker compose -p sagasmith-service-e2e -f compose.yaml -f compose.e2e.yaml up -d --build
uv run python scripts/container_e2e.py
```

Architecture and operating references:

- [`docs/architecture.md`](docs/architecture.md)
- [`docs/threat-model.md`](docs/threat-model.md)
- [`docs/operations.md`](docs/operations.md)
- [`docs/test-matrix.md`](docs/test-matrix.md)
- [`docs/community.md`](docs/community.md)

## Non-negotiable boundary

The control plane stores cloud workflow and projections only. Every authoritative campaign,
membership, actor, phase, revision, combat, random, and Pack activation write goes through the
current public D&D MCP facade and is reconciled from an MCP receipt.
