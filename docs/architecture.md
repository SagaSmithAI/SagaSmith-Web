# Hosted SagaSmith domain architecture

## Product boundary

`SagaSmith-service` is the private control plane and Web product. The Apache-licensed repositories
remain complete local/self-hosted products. Dependency direction is one way:

```text
Browser -> Service API/BFF -> hosted Agent worker -> matching domain MCP facade
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

## Module Studio

Module Studio is the primary D&D creation surface. A hidden Lobby campaign gives each project a
real MCP authorization and revision context, but hidden authoring campaigns never appear in the
normal campaign list. PostgreSQL persists projects, source generations, immutable decisions,
leased tasks, retries, installations, notifications and quota receipts. Sources live in private
object storage and are materialized into the MCP exchange volume only for the duration of import.

The Module worker asks the Hosted Agent to follow the installed `sagasmith-modulegen` Skill and
return strict semantic decisions. The Service transports those explicit decisions to
`module_draft(start|get|evidence|edit|finalize)`. D&D MCP still owns extraction evidence, draft
revision, validation, idempotency, mechanical import and the compiled content artifact. A task
lease survives API restarts; an expired lease is requeued, while cached Agent decisions and stable
MCP idempotency keys prevent duplicate charging or duplicate authoritative writes.

The product states are `idea -> outline_ready -> generating -> draft_review ->
ready_to_finalize -> compiled`; `failed` and `canceled` are resumable. Finalization requires both an
approved evidence review and a fresh explicit Agent confirmation. Compiled artifacts can be
installed directly into owned/DM campaigns or submitted to Forge moderation. Published community
installs import the same MCP artifact and never reconstruct rule or module state in Service.

The Service never opens either system MCP database. Campaign state, phase, random streams, revisions,
idempotency, snapshots, branches, undo/redo, actor scope, settlement, and Pack activation remain
MCP-owned. Service records a receipt and a disposable projection after a successful public tool
call.

## Trust and identity

- Browser authentication is an opaque, hashed, revocable server session in an HttpOnly cookie.
- A stable human MCP principal is derived as `user:<service-user-uuid>`. An accepted hosted
  Identity assignment derives `agent:<identity-uuid>`. Browsers cannot submit either principal.
- Campaign ownership and admission workflow are Service concepts; effective membership and actor
  control are granted by `access_grant` and enforced again by MCP at call time.
- The campaign owner may promote or demote active members between `player` and `dm`; Service calls
  the MCP grant first and updates its role projection only after the authoritative receipt.
- `owner` and `dm` may review applications, create invitations, bind actors, and import/activate
  finalized Packs.
- A player sees only campaigns with an active membership projection. The MCP remains authoritative
  if a projection is stale.
- Administrator status grants control-plane operations such as quota grants; it does not imply DM
  authority in a campaign.

## Dynamic native tools

The browser does not receive or imitate MCP tools. It posts chat or action messages to a shared
campaign room. Ordinary chat is persisted without invoking the Agent; an action starts a
sender-scoped Agent run whose result is appended to the same ordered room timeline. A hosted Agent
worker owns a real MCP client session, consumes the server's current native
`tools/list`, listens for `tools/list_changed`, and refreshes schemas before the next call. Lobby,
Play, Combat and checkout/restore transitions therefore change the real native tool list.

Workers are isolated by campaign and authenticated principal. The private Agent Supervisor starts
a dedicated Nanobot subprocess for each principal's stable room conversation, giving every human
or hosted Identity a separate MCP client and tool registry without mounting the Docker socket.
Shared room context is only the sender-visible message window; it never changes that worker's
principal or actor scope. The cloud topology is:

```text
campaign room -> principal-scoped conversation lease -> dedicated Agent worker
                                              -> dedicated MCP session
                                              -> persisted workspace/session volume
                                              -> idle timeout -> graceful close -> resumable lease
```

Room messages and room events use monotonic per-room sequence numbers in PostgreSQL. The browser
opens an SSE stream after loading a REST snapshot. Message events append to the timeline, while
`state.changed` invalidates the Character, Play, Combat and Module projections. Panel commands use
the same authenticated room action path or a narrow Service-to-MCP facade; panels never write game
tables or reproduce rules. A periodic projection refresh is only recovery for a lost stream, not a
second authority.

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

The Service-injected context contains the authenticated campaign and principal. It is a semantic
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
| users, sessions, quotas, invitations, applications | Service PostgreSQL | PostgreSQL backup |
| campaign/member/actor display projection | Service cache | MCP reconciliation |
| campaign world and mechanic state | D&D/CoC MCP | per-system state backup/snapshot |
| room message/event/read cursor and Agent run/usage receipt | Service + Agent workspace | both backups |
| private Pack archive | private object storage | versioned object backup |
| imported/activated Pack state | system MCP | MCP backup + immutable archive |
| public artifact/release metadata, discussions, reports | Service PostgreSQL | PostgreSQL backup |
| Soul and Identity public profile | Service PostgreSQL | PostgreSQL backup |
| Identity campaign assignment and curated memory | Service PostgreSQL + MCP access grant | PostgreSQL + MCP backup |
| Module project/task/decision/version metadata | Service PostgreSQL | PostgreSQL backup |
| Module source generations | private object storage | versioned object backup |
| Module draft and compiled artifact | D&D MCP | D&D state backup/snapshot |

## Pack lifecycle and copyright

The hosted library accepts finalized SagaSmith Pack archives, not commercial PDFs. Upload requires
an explicit right-to-store attestation, is always `distribution=private`, streams through a size
limit, receives a SHA-256 digest, and has no public download route. Import materializes a short-lived
copy into a volume shared only with D&D MCP, then calls `content_pack(import)` in Lobby. Import
receipts supply the runtime reference; activation is a distinct idempotent `content_pack(activate)`
call. Service changes its projection to `activated` only after that authoritative receipt.

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

- Campaign creation passes a Service-derived idempotency key to MCP. Retrying after a projection
  write failure receives the same authoritative campaign and repairs the projection.
- Join and actor grants write the Service projection only after an MCP receipt.
- Agent calls reserve quota before provider execution, settle actual tokens afterward, and release
  on failure. Both reservation and settlement are idempotent.
- Pack import uses campaign, Pack id and archive checksum as its MCP idempotency key.
- Pack activation uses a caller idempotency key scoped by campaign and immutable Pack identity.
- PostgreSQL row locks serialize invitation use, join decisions and quota balance changes.

Campaign removal uses the public `access_revoke` facade. Service calls that authority first, then
revokes its membership/actor projections and closes the removed user's active conversations. A
failed MCP revocation leaves every Service projection active, so the control plane can never claim
that access was removed when the authority still permits it.
