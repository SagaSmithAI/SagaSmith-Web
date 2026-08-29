# SagaSmith Web

[简体中文](README.zh-CN.md) · [Website](https://sagasmithai.github.io) ·
[Platform overview](https://github.com/SagaSmithAI/.github/blob/main/profile/README.md) ·
[Public content catalog](https://github.com/SagaSmithAI/SagaSmith-dnd-content-library) ·
[All repositories](https://github.com/orgs/SagaSmithAI/repositories) · [Security](SECURITY.md) ·
[Operations](docs/operations.md)

SagaSmith Web is the hosted browser product and control plane for SagaSmith. It combines the PWA,
FastAPI API/BFF, campaign collaboration, live rooms, Forge, Module Studio, Hosted Agent
orchestration, and deployment operations in one repository.

This repository is publicly visible, but its source remains subject to the proprietary terms in
[LICENSE](LICENSE). The project is an active Alpha. The current component lock is a reviewed
compatibility lock and is not itself a published release.

Historical implementation identifiers remain stable: the Python package is
`sagasmith_service`, the distribution and CLI are `sagasmith-service`, and the default Compose
project is `sagasmith-service`. The product name is **SagaSmith Web**.

## What is included

```text
SagaSmith Web
├── Browser frontend and installable PWA
├── FastAPI API/BFF and account control plane
├── Campaign admission, collaboration, live rooms, and realtime projections
├── Durable RoomTurnJob and Module Studio workers
├── Hosted Agent supervision, identity, media, and workspace lifecycle
├── SagaSmith Forge catalog, review, moderation, and installation
└── PostgreSQL, Redis, private object storage, proxy, backup, and observability
```

The matching domain MCP remains authoritative for game state. SagaSmith Web owns hosted workflow
and audience-safe projections; it does not become a second D&D, CoC, or Narrative database.

### Product surfaces

- **Accounts and campaigns:** registration, legal acceptance, session lifecycle, invitations,
  join approval, role changes, revocation, plan/quota accounting, and administrative audit.
- **Live rooms:** shared and audience-filtered chat, private character cards, synchronized
  Character/Play/Combat/Module panels, player intents, realtime timeline, and tactical grid.
- **Forge:** Rule/Module Packs, character blueprints, Souls, Skills, assets, and hosted Identities
  share catalog, version, provenance, license, discussion, favorite, report, and moderation
  primitives. Published releases are immutable.
- **Module Studio:** brief/source intake, outline approval, persistent Agent generation,
  MCP-owned evidence review/edit, explicit finalization, immutable compile, import, and optional
  activation. Pack is the internal delivery artifact rather than a user-facing authoring concept.
- **Hosted Identities:** a DM/Keeper Identity pins one published Soul release and receives an
  explicit campaign assignment, quota payer, MCP role, and campaign-isolated revisioned memory.

Agent review is evidence, not publication authority. Administrator moderation remains a separate
step, and private or commercial source cannot enter the public catalog.

## Local Kit and Hosted Web

SagaSmith has two deployment shapes that share one domain contract:

| Concern | Local Agent Kit | Hosted Web |
|---|---|---|
| Entry point | SagaSmith Agent, another MCP Host, or a bot | Browser/PWA through the Web API/BFF |
| Domain transport | stdio or localhost Streamable HTTP | D&D/CoC network Streamable HTTP; Narrative process-local stdio |
| Identity | local Host policy | server session plus request-scoped signed delegation |
| Storage | SQLite and local files | PostgreSQL, Redis, private object storage, isolated domain state |
| Cloud account/quota/Forge | not required | owned by SagaSmith Web |
| Rules authority | matching domain MCP | the same matching domain MCP |

The open Local Agent Kit never depends on this repository. Local and hosted deployments use the
same domain handlers, schemas, revision and idempotency rules, structured results, and authority
semantics; only transport, authentication, storage, and deployment differ.

### Current source repositories

| Layer | Repository |
|---|---|
| Agent Host | [`SagaSmith-agent`](https://github.com/SagaSmithAI/SagaSmith-agent) |
| Neutral runtime | [`sagasmith-core`](https://github.com/SagaSmithAI/sagasmith-core) |
| D&D domain, MCP, Skills, and UI | [`sagasmith-dnd`](https://github.com/SagaSmithAI/sagasmith-dnd) |
| CoC domain, MCP, Skills, and UI | [`sagasmith-coc`](https://github.com/SagaSmithAI/sagasmith-coc) |
| Narrative domain, MCP, and Skills | [`sagasmith-narrative`](https://github.com/SagaSmithAI/sagasmith-narrative) |
| Audited public content catalog | [`SagaSmith-dnd-content-library`](https://github.com/SagaSmithAI/SagaSmith-dnd-content-library) |

Former standalone MCP, Skills, UI, and generic Module Generator repositories are archived. They
are not release inputs, compatibility fallbacks, or destinations for new work.

## Reviewed component lock

The hosted build is reproducible from [`component-versions.json`](component-versions.json), schema
`sagasmith.release-lock/v3`, lock `2026.8.30-mcp-modern-final`:

| Enforced component | Reviewed revision |
|---|---|
| SagaSmith Agent | `c0731c44775a045bb106000391ef6240f7b4d2a3` |
| SagaSmith Core | `612bfe7e5290eb5b23f2811baa83b8a28293b36e` |
| D&D | `ba602ea103de67d19ffbfba2f69f3802a9e34dd3` |
| CoC | `eebab0986299b0cd9ce420c3521e4688356e9746` |
| Narrative | `2e2dfe8a18b53ad4c090a7382c25c32a354c7621` |

The lock requires MCP `2026-07-28`, `sagasmith.authoritative-mcp/v2`,
`sagasmith.auth-context/v2`, and the modern Hosted boundary. `compose.yaml`, `.env.example`, the
Agent configuration, and this manifest must move together. Use the workspace override only for
coordinated development; production uses the immutable remote revisions in `compose.yaml`.

## Authority and trust boundaries

SagaSmith Web owns accounts, sessions, plans, quota reservations and usage, invitations, hosted
process orchestration, Forge, Module Studio workflow, room messages, and cloud projections. A
domain MCP exclusively owns campaign membership, actor authority, phase/combat/random state,
revisions, idempotency, atomic settlement, and Pack activation.

The modern Hosted path separates trusted authority from user content:

- The browser sends text and an idempotency key; it never chooses an authoritative principal.
- Web derives `user:<uuid>` from the server-side session. An accepted hosted Identity assignment
  may supply an acting `agent:<uuid>`, but the model cannot select either identity.
- `sagasmith.auth-context/v2` records caller/workload identity, requester, resource owner, acting
  Host/character, exact target and audience, allowed operations, campaign, room turn,
  `base_revision`, and expiry.
- Web signs a target-specific delegation for the Agent. Browser or provider tokens are never
  forwarded to an MCP, and each MCP request is authorized again.
- Player text and trusted context remain separate structured fields. Prompt text is not a security
  boundary.

See [the architecture](docs/architecture.md) and [threat model](docs/threat-model.md) for the full
ownership and data-flow contract.

## MCP 2026-07-28 and bounded tool selection

Modern mode is the hosted default. It uses `server/discover`, per-request protocol and capability
metadata, and per-request authorization without relying on `initialize`, `Mcp-Session-Id`, or a
connection-scoped principal. Cross-call state uses explicit campaign/revision inputs or a
server-issued opaque handle with an owner and TTL.

Tool management deliberately avoids sending the full catalog to the model:

1. the Agent connects only the MCP matching the campaign `system_id`;
2. the MCP publishes a deterministic, sorted, authorization-scoped private catalog;
3. Web selects the exact system/phase/role/task facade and persists at most 16 sorted, unique tool
   IDs with the durable turn; and
4. the domain MCP still checks role, phase, revision, idempotency, and authority on every call.

The 16-tool limit is a SagaSmith Host policy, not an MCP protocol limit. Catalog projection improves
selection quality and cacheability; it never replaces server-side authorization. Session-mutated
tool exposure and `tools/list_changed` are not authoritative in modern mode.

Legacy mode exists only for an atomic rollback of Web, Agent, and all three domains to a compatible
lock. Do not mix modern and legacy components or use legacy prompt fields as an identity boundary.

### MCP Tasks

`RoomTurnJob` is a Web Host job for an entire LLM turn. It is not an MCP Task. The negotiated
`io.modelcontextprotocol/tasks` extension is used only when a genuinely long domain tool returns a
Task claim, currently the D&D module-draft start workflow. Ordinary tools remain synchronous and
bounded by `toolTimeout`; only an accepted Task claim switches to `taskTimeout` and authenticated
poll/cancel/recovery.

## Durable room turns

Every room action atomically persists the user message and a `RoomTurnJob`. The durable state
machine is:

```text
queued -> running -> succeeded
            |            terminal
            +-> waiting -> retry publication/settlement
            +-> failed
            +-> cancelled
```

Workers claim jobs with a lease, heartbeat while active, classify errors, use a bounded retry
budget, and recover expired `running`/`waiting` work at startup and periodically. Browser, Web,
Agent, and MCP retries reuse the same business idempotency key. If the Agent/MCP operation commits
but Web publication fails, the saved standard MCP `CallToolResult` is reused; the operation is not
executed or charged twice.

The action may carry `base_revision`. Web preloads the authoritative phase/revision without
holding a room or database lock, then performs a short compare-and-set. A stale revision returns a
recoverable HTTP 409. Only final ordered message/outbox settlement takes a short per-room lock, so
independent rooms, reads, and compatible actions can continue concurrently.

`SAGASMITH_ROOM_TURN_WORKER_CONCURRENCY` bounds the process-wide worker pool, while
`SAGASMITH_ROOM_TURN_PER_ROOM_CONCURRENCY` (default `4`) independently limits expensive turns for
one room. The durable claim transaction locks the room row, counts valid leases, and releases the
lock before Agent/MCP work, so the limit holds across Web replicas without a long database or
settlement lock. Set the per-room value to `1` when strict single-turn execution is preferred,
without serializing unrelated rooms.

The reservation TTL must exceed the Agent completion timeout. Defaults are 1,200 and 900 seconds,
respectively. Job heartbeats renew the reservation; an expired timestamp alone cannot free quota
while an active room or Module job owns it. Usage settles immediately after Agent completion and
before retryable Web publication.

Clients can recover or cancel work through:

- `GET /api/campaigns/{campaign_id}/room/jobs/{job_id}`
- `POST /api/campaigns/{campaign_id}/room/jobs/{job_id}/cancel`

Terminal errors retain their stable code, retryability, class, and recovery hint. Conflicts and
cancellation return 409, model-correctable request/tool-output errors return 422, retryable
Agent/storage failures return 503, non-retryable upstream failures return 502, and unexpected Host
faults return 500. A network retry uses the original idempotency key for every retryable class.

Operational details are in [Durable room-turn operations](docs/room-turn-jobs.md).

## Hosted Agent results, media, and workspaces

The Hosted Worker preserves standard MCP text, image, audio, resource, and embedded-resource
content. Web stores the original `CallToolResult`, then projects accepted media internally into a
`sagasmith.host-media/v1` envelope and private object ID with room-audience checks, size limits,
checksums, and idempotent artifact keys. This supports room/group images and combat grids without
inventing a replacement MCP wire protocol.

Each conversation runs in a bounded worker process. The Supervisor limits worker count and spawn
concurrency, coalesces simultaneous starts, and returns 503 instead of creating unbounded
processes. Managed state lives only below `/workspaces/hosted-v1` and uses an owner marker plus an
opaque workspace ID. Startup recovers crash-left markers, terminal success removes registered
state, and TTL/LRU cleanup enforces count and byte limits. The supervisor recognizes Agent's exact
regular-file root admission lock as operational metadata; every other unknown, malformed, external,
legacy, symlinked, or active entry is preserved for operator review rather than deleted.

## Projections, caches, and realtime delivery

Web does not read a domain's authoritative database directly. MCP receipts drive revisioned,
audience-safe Web projections and a durable outbox. Projection cache keys include authority
revision; successful commits invalidate only affected scopes, while failed, rolled-back, and no-op
operations do not invalidate data. Tool-catalog caching changes with authorization/catalog scope,
not with each combat write.

Each committed `state.changed` receipt and its outbox wake-up carry `authority_revision`, sorted
`changed_scopes`, affected `entity_ids`, and the audience descriptor. Consumers can therefore
rebuild or invalidate only the revisioned projection named by the receipt. If a tool receipt does
not advance the authority revision, Web emits no state-change invalidation.

Room and Module SSE use Redis wake-ups plus PostgreSQL cursor replay. The database reconciliation
poll is a missed-event safety net. Structured activity/suggestion fields and composite indexes
avoid scanning full campaign history on each request.

## Local development

Prerequisites are Python 3.12, `uv`, Docker with Compose, and Git. For the workspace build, check
out the five source repositories shown above as sibling directories with the names used by
`compose.workspace.yaml`. The public content library is also mounted by that override.

Prepare configuration:

```powershell
Copy-Item .env.example .env
New-Item -ItemType Directory -Force secrets
Copy-Item config/agent-config.example.json secrets/agent-config.json
```

Replace every `replace-*` value, set `OPENAI_API_KEY`, and use independent random values of at
least 32 bytes for `SAGASMITH_SESSION_SECRET`, `SAGASMITH_WORKER_SERVICE_TOKEN`, and
`SAGASMITH_AUTH_CONTEXT_SECRET`. Keep `.env` and `secrets/` out of Git.

Start the coordinated sibling-worktree build:

```powershell
docker compose -f compose.yaml -f compose.workspace.yaml config
docker compose -f compose.yaml -f compose.workspace.yaml up --build
```

The API health endpoint through Caddy is `http://127.0.0.1/api/health`; readiness is
`http://127.0.0.1/api/ready`.

Run the fast local checks:

```powershell
uv sync --frozen --all-extras
uv run ruff check .
uv run pytest
```

The async hot-path harness uses a disposable SQLite database by default:

```powershell
uv run python scripts/benchmark_async_hotpaths.py --concurrency 4 --iterations 5
```

See [Async database hot paths](docs/async-database-hotpaths.md) before using its explicitly gated,
disposable PostgreSQL mode. Never benchmark with production campaign data.

## Container acceptance

The hermetic acceptance stack uses a deterministic local OpenAI-compatible test provider while
running the real pinned Agent and domain components:

```powershell
docker compose -p sagasmith-service-e2e -f compose.yaml -f compose.e2e.yaml up -d --build --wait --wait-timeout 300
uv run python scripts/container_e2e.py
uv run python scripts/container_fault_e2e.py
docker compose -p sagasmith-service-e2e -f compose.yaml -f compose.e2e.yaml down --volumes --remove-orphans
```

It covers D&D, CoC, and Narrative discovery/calls; requester versus acting-Host identity; the
bounded tool facade; quota and idempotency; Module Studio and Pack activation; Redis, MCP, Agent,
and worker restart recovery; workspace cleanup; revocation; and audit receipts. The complete CI
and release evidence is listed in [the acceptance matrix](docs/test-matrix.md).

## Production deployment, upgrade, and rollback

Production uses `compose.yaml` alone, so Docker builds the exact remote revisions in the component
lock. Copy the example files as above, provide real secrets and a model credential, then verify the
candidate before starting it:

```powershell
uv run python scripts/audit_components.py --fetch --strict
uv run python scripts/audit_components.py --scope build --strict --json
docker compose config
docker compose up -d --build
docker compose ps
```

Set a real `SAGASMITH_SITE_ADDRESS`, `SAGASMITH_PUBLIC_ORIGIN`, secure cookies, private object
storage credentials, and HTTPS before serving users. Only Caddy ports 80/443 should be public;
PostgreSQL, Redis, MinIO, MCPs, Agent, worker metrics, and `/metrics` remain private.

Before upgrading, back up PostgreSQL, object storage, D&D/CoC state, and Agent workspaces; verify
the manifest; fetch and audit the new component lock; and run `alembic upgrade head` before
admitting new workers. When introducing `room_turn_jobs`, drain old replicas first because they do
not understand the new durable state.

For rollback, stop new action admission, let active jobs become terminal, retain the database and
object backup, and roll Web, Agent, and all domains back as one compatible lock. Do not downgrade
the room-job migration while retained jobs or media are needed, and do not switch only one
component to legacy mode. Full backup, restore-drill, release, and rollback procedures live in
[Operations and recovery](docs/operations.md).

## Observability

- `/api/health` reports liveness; `/api/ready` checks PostgreSQL, Redis/rate limiting, private
  object storage, Agent, and all domain runtimes.
- `/metrics` exposes low-cardinality service, MCP phase, projection, durable-job, quota, database,
  outbox, and realtime metrics. User, campaign, room, job, and tool arguments are not metric labels.
- `traceparent`, `tracestate`, and `baggage` propagate through Web, Agent, and MCP calls and are
  retained with the durable job in bounded fields.
- `module-worker:9101/metrics` and `agent:8910/metrics` stay on the private network.

The optional local observability profile adds Prometheus, Grafana, Loki, Tempo, an OTLP Collector,
and Alloy:

```powershell
docker compose -f compose.yaml -f compose.observability.yaml --profile observability up -d
```

Set `GRAFANA_ADMIN_PASSWORD` and protect every dashboard/ingestion endpoint before changing its
loopback binding. See [Operations and recovery](docs/operations.md#health-and-observability) for
metric names, alerts, retention, and runtime constraints.

## Security essentials

- Never commit `.env`, `secrets/`, provider credentials, private Packs, commercial source, campaign
  exports, backup contents, or worker state.
- Browser identities, prompts, Souls, and model output cannot grant MCP authority.
- Public releases reject private/commercial source and executable rules; publication is immutable
  and administrator moderation is separate from Agent review.
- Backups are portable but are not encrypted by the backup script. Store completed backups in
  encrypted off-host storage and escrow deployment secrets separately.
- Report vulnerabilities privately through the process in [SECURITY.md](SECURITY.md). Do not test
  against another person's campaign or a production deployment without authorization.

## Documentation map

- [Architecture and authority](docs/architecture.md)
- [Durable room-turn operations](docs/room-turn-jobs.md)
- [Operations, deployment, backup, and rollback](docs/operations.md)
- [Threat model](docs/threat-model.md)
- [Acceptance matrix](docs/test-matrix.md)
- [Forge and community](docs/community.md)
- [Frontend modules](docs/frontend-modules.md)
- [Async database hot paths](docs/async-database-hotpaths.md)
- [Repository rename record](docs/repository-rename-checklist.md)
