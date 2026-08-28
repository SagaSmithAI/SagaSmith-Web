# SagaSmith Web architecture

## Product boundary

**SagaSmith Web** is the hosted product in `SagaSmithAI/SagaSmith-Web`.
The control plane is one backend responsibility alongside the browser frontend, API/BFF,
collaboration, Forge, Module Studio, Agent orchestration, and operations. The Apache-licensed
repositories form the complete Local Agent Kit, so dependency direction is one way:

```text
Browser -> SagaSmith Web API/BFF -> hosted Agent worker -> matching domain MCP facade
                           -> PostgreSQL (cloud workflow/projections/usage)
                           -> private S3/MinIO (Pack archives)

Browser -> Forge catalog -> Artifact + immutable Release + discussion/moderation
                         -> Soul -> hosted Identity -> campaign assignment

sagasmith-dnd       -> packages/mcp + packages/domain + Skills + UI
sagasmith-coc       -> packages/mcp + packages/domain + Skills + UI
sagasmith-narrative -> packages/mcp + packages/domain + Skills
all domains         -> sagasmith-core

D&D and CoC MCP run as isolated network services. Narrative advertises
`shared_network_transport_supported=false`, so each hosted Agent worker starts its own stdio
Narrative MCP process instead of exposing a compatibility HTTP wrapper.
```

The FastAPI lifespan owns one long-lived HTTP connection pool for each Agent, D&D, CoC and
Narrative adapter. Connection reuse never carries a principal, campaign, authorization decision,
or tool exposure: modern MCP requests carry protocol version, capability, trace and target-specific
delegation on every request. A temporary legacy `ClientSession` exists only on the documented
dual-era compatibility path and is never pooled across principals.

SagaSmith Web also owns a selective SQLAlchemy `AsyncEngine`/`AsyncSession` stack for room messages,
panel intents and panel projection refreshes. An action transaction commits the user message and a
durable `RoomTurnJob`, then releases the request transaction. A leased worker creates or resumes the
quota reservation and Agent run, performs Agent/MCP work without holding a database transaction,
and reacquires a short per-room settlement lock only to publish ordered messages and outbox events.
Panel refreshes follow the same read-before-await/write-after-await boundary. This is a measured
hot-path design, not a second authority or a repository-wide ORM rewrite.

## Shared authority across local and hosted deployments

```text
Local Agent Kit                           Hosted Web Product

Bot / generic Agent / SagaSmith Agent     Browser / PWA
                  |                             |
                  | MCP                         v
                  |                       SagaSmith Web API/BFF
                  |                             |
                  |                       principal-scoped Agent worker
                  |                             |
                  +---------- domain MCP <-----+
                               |
                        matching domain runtime
                               |
                     authoritative campaign state
```

The two deployment shapes must execute the same MCP handlers and schemas. Local installations may
use stdio or localhost Streamable HTTP with SQLite and local files; hosted deployments may use
signed principals, network MCP, PostgreSQL, Redis, and object storage. Those differences must not
create a second implementation of rules, state writes, tool semantics, revision checks, or
idempotency. SagaSmith Agent is an MCP consumer and host in both shapes, never the owner of domain
state.

## Module Studio

Module Studio is the primary D&D creation surface. A hidden Lobby campaign gives each project a
real MCP authorization and revision context, but hidden authoring campaigns never appear in the
normal campaign list. PostgreSQL persists projects, source generations, immutable decisions,
leased tasks, retries, installations, notifications and quota receipts. Sources live in private
object storage and are materialized into the MCP exchange volume only for the duration of import.

The Module worker asks the Hosted Agent to follow the installed `sagasmith-modulegen` Skill and
return strict semantic decisions. SagaSmith Web transports those explicit decisions to
`module_draft(start|get|evidence|edit|finalize)`. D&D MCP still owns extraction evidence, draft
revision, validation, idempotency, mechanical import and the compiled content artifact. A task
lease survives API restarts; an expired lease is requeued, while cached Agent decisions and stable
MCP idempotency keys prevent duplicate charging or duplicate authoritative writes.

The product states are `idea -> outline_ready -> generating -> draft_review ->
ready_to_finalize -> compiled`; `failed` and `canceled` are resumable. Finalization requires both an
approved evidence review and a fresh explicit Agent confirmation. Compiled artifacts can be
installed directly into owned/DM campaigns or submitted to Forge moderation. Published community
installs import the same MCP artifact and never reconstruct rule or module state in SagaSmith Web.

SagaSmith Web never opens any domain MCP database. Campaign state, phase, random streams, revisions,
idempotency, snapshots, branches, undo/redo, actor scope, settlement, and Pack activation remain
MCP-owned. SagaSmith Web records a receipt and a disposable projection after a successful public tool
call.

## Trust and identity

- Browser authentication is an opaque, hashed, revocable server session in an HttpOnly cookie.
- A stable human MCP principal is derived as `user:<service-user-uuid>`. An accepted hosted
  Identity assignment derives `agent:<identity-uuid>`. Browsers cannot submit either principal.
- Campaign ownership and admission workflow are SagaSmith Web concepts; effective membership and actor
  control are granted by `access_grant` and enforced again by MCP at call time.
- The campaign owner may promote or demote active members between `player` and `dm`; SagaSmith Web calls
  the MCP grant first and updates its role projection only after the authoritative receipt.
- `owner` and `dm` may review applications, create invitations, bind actors, and import/activate
  finalized Packs.
- A player sees only campaigns with an active membership projection. The MCP remains authoritative
  if a projection is stale.
- Administrator status grants control-plane operations such as quota grants; it does not imply DM
  authority in a campaign.

## Stable native tool catalogs

The browser does not receive or imitate MCP tools. It posts chat or action messages to a shared
campaign room. Ordinary chat is persisted without invoking the Agent; an action starts a
sender-scoped Agent run whose result is appended to the same ordered room timeline. For the same
authorization, modern `tools/list` is deterministic, sorted and cacheable with explicit TTL/cache
scope. The Host may choose a task/phase-appropriate subset for the model, but no exposure side
effect mutates the server catalog and every tool call revalidates role, phase and revision. Legacy
`tools/list_changed` behavior is isolated to the compatibility path and is not an authority
boundary.

Workers are isolated by campaign and authenticated principal. The private Agent Supervisor starts
a dedicated Nanobot subprocess for each principal's stable room conversation, giving every human
or hosted Identity a separate MCP client and tool registry without mounting the Docker socket.
Shared room context is only the sender-visible message window; it never changes that worker's
principal or actor scope. The cloud topology is:

```text
campaign room -> principal-scoped conversation lease -> dedicated Agent worker
                                              -> per-request MCP authority context
                                              -> persisted workspace/session volume
                                              -> idle timeout -> graceful close -> resumable lease
```

`RoomTurnJob` is a Web Host orchestration record, never an MCP Task. Its durable states are
`queued`, `running`, `waiting`, `succeeded`, `failed`, and `cancelled`; leases, heartbeats,
bounded retries, saved standard Agent/MCP results and stable idempotency keys recover safely after
a worker or Web restart. MCP Tasks are negotiated only for one genuinely long-running domain tool.

Room messages and room events use monotonic per-room sequence numbers in PostgreSQL. The browser
opens an SSE stream after loading a REST snapshot. Message events append to the timeline, while
`state.changed` invalidates the Character, Play, Combat and Module projections. Panel commands use
the same authenticated room action path or a narrow SagaSmith Web-to-MCP facade; panels never write game
tables or reproduce rules. A periodic projection refresh is only recovery for a lost stream, not a
second authority.

Committed room, campaign-access, actor-binding and Module changes also create a transactional
outbox row in the same PostgreSQL transaction. One process-level dispatcher publishes those rows
through Redis and one process-level subscription fans them out to local SSE clients. A stream
subscribes before its initial database replay, then queries only after an event wake-up or a
30-second reconciliation deadline. Redis is a wake-up and fan-out layer, never the recovery
authority; reconnecting clients replay the durable room sequence from PostgreSQL.

Panel cache entries are versioned principal/audience projections, not copies of MCP tables. Their
identity includes campaign, audience, source revision, authorization epoch and projection schema
version. A relevant MCP receipt invalidates or replaces only the affected projection; a write does
not synchronously rebuild every user's cache. Membership or actor-authority changes increment or
invalidate the affected authorization scope before an old projection can be reused.

The live room uses one shared, persistent composer with three synchronized surfaces: a collapsible
left character drawer, the central room timeline, and right scene/combat/module/member panels. The
character drawer switches between the full private card, spells, equipment/inventory and an
audience-safe party summary. Full cards are fetched separately through `character_query(view=get)`
only for DM or an actor binding with `can_view_private`; other characters are never delivered to
the browser as hidden DOM data. Selecting a character for inspection is distinct from selecting the
actor that will perform the next action.

Grid Combat is rendered only when the MCP audience projection says the active encounter uses
`positioning_mode=grid` and includes a battle map. The expanded map reuses the same composer DOM,
so its draft and selected audience survive expansion. Token, target and destination selections are
sent to the Agent as declared action context, not authoritative facts; the Agent must validate them
through MCP. Agent-positioned Combat never synthesizes coordinates or a fallback grid.

The SagaSmith Web-injected context contains the authenticated campaign and principal. It is a semantic
aid only; every MCP call remains fail-closed on membership, actor, phase, revision and payload.
The Supervisor requires either `campaign:user:conversation` or
`campaign:agent:identity:conversation` to match its authenticated principal. The hosted worker maps
the principal prefix to Nanobot's trusted inbound channel, so MCP sees exactly `user:<uuid>` or
`agent:<uuid>`. Internal MCP DNS names are
resolved to exact host CIDRs in an ephemeral mode-0600 worker config; no broad private-network SSRF
exception is granted.

## Data ownership

| Data | Authority | Recovery source |
|---|---|---|
| users, sessions, quotas, invitations, applications | SagaSmith Web PostgreSQL | PostgreSQL backup |
| campaign/member/actor display projection | SagaSmith Web cache | MCP reconciliation |
| principal-scoped panel projection | SagaSmith Web cache keyed by MCP revision and authorization epoch | MCP snapshot + outbox reconciliation |
| campaign world and mechanic state | D&D/CoC MCP | per-system state backup/snapshot |
| room message/event/read cursor and Agent run/usage receipt | SagaSmith Web + Agent workspace | both backups |
| private Pack archive | private object storage | versioned object backup |
| imported/activated Pack state | system MCP | MCP backup + immutable archive |
| public artifact/release metadata, discussions, reports | SagaSmith Web PostgreSQL | PostgreSQL backup |
| Soul and Identity public profile | SagaSmith Web PostgreSQL | PostgreSQL backup |
| Identity campaign assignment and curated memory | SagaSmith Web PostgreSQL + MCP access grant | PostgreSQL + MCP backup |
| Module project/task/decision/version metadata | SagaSmith Web PostgreSQL | PostgreSQL backup |
| Module source generations | SagaSmith Web private object storage | versioned object backup |
| Module draft and compiled artifact | D&D MCP | D&D state backup/snapshot |

## Pack lifecycle and copyright

The hosted library accepts finalized SagaSmith Pack archives, not commercial PDFs. Upload requires
an explicit right-to-store attestation, is always `distribution=private`, streams through a size
limit, receives a SHA-256 digest, and has no public download route. Import materializes a short-lived
copy into a volume shared only with D&D MCP, then calls `content_pack(import)` in Lobby. Import
receipts supply the runtime reference; activation is a distinct idempotent `content_pack(activate)`
call. SagaSmith Web changes its projection to `activated` only after that authoritative receipt.

Source authoring remains `draft -> Agent evidence review -> finalize`. Draft source, extracted text,
chunks and embeddings stay in private storage. Forge publication is a second trust boundary:
`release draft -> hosted Agent review -> moderation_pending -> published`. Published releases are
immutable; edits create a new release. `private_source` and `contains_private_source` releases
cannot enter moderation, executable Rule payloads are rejected, and commercial source archives
remain absent from public catalog and download surfaces.

## Artifact, Soul and Identity

An Artifact is a shareable work; a Release is its immutable version. Character artifacts are
blueprints, not live actors. A Soul is semantic guidance, not an MCP authority or fixed tool list.
An Identity is a persistent hosted subject pinned to one published Soul release. A campaign
assignment freezes that Soul release, quota payer, MCP role and a unique memory namespace. Identity
memory entries are revisioned and can be read or written only by campaign DMs or the Identity
owner. They never flow back into the public Soul or another campaign.

Rule/Module/Character releases require a validated current Pack. Installing them materializes a
short-lived archive and calls D&D MCP import; Rule/Module activation remains a distinct call.
Soul/Skill/Asset releases install as library references and never mutate campaign authority.

## Consistency

- Campaign creation passes a SagaSmith Web-derived idempotency key to MCP. Retrying after a projection
  write failure receives the same authoritative campaign and repairs the projection.
- Join and actor grants write the SagaSmith Web projection only after an MCP receipt.
- Every committed Web-visible projection change writes its outbox signal in the same transaction;
  cache rebuilding and Redis delivery happen after commit and can be retried safely.
- Agent calls reserve quota before provider execution, renew the reservation with the worker lease,
  settle actual tokens before Web publication, and explicitly expire only abandoned reservations.
  Reservation and settlement are idempotent, and an expired timestamp alone never creates an
  over-allocation window while a durable job remains active.
- Pack import uses campaign, Pack id and archive checksum as its MCP idempotency key.
- Pack activation uses a caller idempotency key scoped by campaign and immutable Pack identity.
- PostgreSQL row locks serialize invitation use, join decisions and quota balance changes.

Campaign removal uses the public `access_revoke` facade. SagaSmith Web calls that authority first, then
revokes its membership/actor projections and closes the removed user's active conversations. A
failed MCP revocation leaves every SagaSmith Web projection active, so the control-plane layer can never claim
that access was removed when the authority still permits it.
