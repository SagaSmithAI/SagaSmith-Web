# SagaSmith Service

Private hosted SagaSmith product. The public SagaSmith repositories remain complete local and
self-hosted applications; this repository adds the hosted account, quota, campaign-lobby,
orchestration, unified Web, billing, and operations layers without becoming an authority for D&D
game state.

## Current target

The first production slice proves the complete trust chain:

1. two users register and sign in;
2. one creates a D&D campaign and becomes owner/DM in the authoritative MCP;
3. the second applies and is approved as a player;
4. a PC is bound to that player through MCP authority;
5. an Agent request reserves quota, records actual usage, and releases the remainder;
6. revocation immediately removes the player's next legal native MCP call;
7. private Pack data remains private through upload, draft review, finalization, and activation.

## Local development

```powershell
Copy-Item .env.example .env
docker compose up --build
```

API health: `http://127.0.0.1:8080/api/health`

Run tests directly:

```powershell
uv sync --all-extras
uv run pytest
uv run ruff check .
```

Architecture and operating references:

- [`docs/architecture.md`](docs/architecture.md)
- [`docs/threat-model.md`](docs/threat-model.md)
- [`docs/operations.md`](docs/operations.md)
- [`docs/test-matrix.md`](docs/test-matrix.md)

## Non-negotiable boundary

The control plane stores cloud workflow and projections only. Every authoritative campaign,
membership, actor, phase, revision, combat, random, and Pack activation write goes through the
current public D&D MCP facade and is reconciled from an MCP receipt.
