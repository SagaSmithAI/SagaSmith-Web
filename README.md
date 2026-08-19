# SagaSmith Service

[Website](https://sagasmithai.github.io) · [Platform overview](https://github.com/SagaSmithAI/.github/blob/main/profile/README.md) · [Public content repository](https://github.com/SagaSmithAI/SagaSmith-dnd-content-library) · [All repositories](https://github.com/orgs/SagaSmithAI/repositories)

Publicly visible source for the hosted SagaSmith product. Repository visibility does not change
the proprietary terms in [LICENSE](LICENSE). The open local and self-hosted SagaSmith repositories
remain complete applications; this repository adds hosted accounts, quota, campaign rooms, Agent
orchestration, unified Web, billing, and operations without becoming an authority for D&D, CoC,
or Narrative game state.

The same deployment includes **SagaSmith Forge**, the account-scoped creation and sharing layer for
Rule/Module Packs, character blueprints, Souls, Skills, assets, and hosted DM/Keeper Identities.
Forge shares catalog, immutable release, license, provenance, discussion, favorite, report, and
moderation primitives while keeping each artifact type's installation semantics explicit.

## Current source inputs

| Layer | Current repository |
|---|---|
| Agent host | [`SagaSmith-agent`](https://github.com/SagaSmithAI/SagaSmith-agent) |
| Neutral runtime | [`sagasmith-core`](https://github.com/SagaSmithAI/sagasmith-core) |
| D&D Domain / MCP / Skills / UI | [`sagasmith-dnd`](https://github.com/SagaSmithAI/sagasmith-dnd) |
| CoC Domain / MCP / Skills / UI | [`sagasmith-coc`](https://github.com/SagaSmithAI/sagasmith-coc) |
| Narrative Domain / MCP / Skills | [`sagasmith-narrative`](https://github.com/SagaSmithAI/sagasmith-narrative) |

Former standalone MCP, Skills, UI, and generic Module Generator repositories
are archived and are not release inputs or compatibility fallbacks.

## Current target

The first production slice proves the complete trust chain:

1. two users register and sign in;
2. one creates D&D and CoC campaigns and becomes owner/DM in each authoritative MCP;
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
docker compose -f compose.yaml -f compose.workspace.yaml up --build
```

The workspace override builds the coordinated sibling worktrees. A production/release deployment
uses `compose.yaml` alone, whose public inputs are immutable revisions from
`component-versions.json`.

API health through Caddy: `http://127.0.0.1/api/health`

Run tests directly:

```powershell
uv sync --all-extras
uv run pytest
uv run ruff check .
```

The API image uses this repository's `uv.lock`. The combined hosted Agent supervisor uses the
Service-owned, hash-locked `infrastructure/agent-supervisor-requirements.txt`, generated from both
Service and Agent constraints. This is an integrated deployment lock, not a third reusable package.

The real container acceptance suite is intentionally separate from unit tests. It boots
PostgreSQL, Redis, MinIO, the D&D and CoC network MCP servers, real Nanobot workers, the persistent Module
Studio worker and a deterministic
OpenAI-compatible test provider, then verifies account/lobby, authenticated Agent identity,
system-directed dynamic native-tool refresh and execution for D&D and CoC, exact quota settlement,
finalized Pack upload/import/
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
- [`docs/component-audit-2026-08-14.md`](docs/component-audit-2026-08-14.md)
- [`docs/component-audit-2026-08-16.md`](docs/component-audit-2026-08-16.md)

Audited public build inputs are pinned in [`component-versions.json`](component-versions.json).
Before a release, fetch and verify every sibling repository with:

```powershell
uv run python scripts/audit_components.py --fetch --strict
```

## Non-negotiable boundary

The control plane stores cloud workflow and projections only. D&D and CoC run as isolated network
domain services. Narrative runs as a process-local stdio domain for each Agent worker because its
capability contract explicitly rejects shared network transport. Every authoritative campaign,
membership, actor, phase, revision, combat, random, and Pack activation write goes through the
matching domain MCP facade and is reconciled from an MCP receipt.
