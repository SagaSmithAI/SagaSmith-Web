# Hosted D&D architecture

## Product boundary

`SagaSmith-service` is the private control plane and Web product. The Apache-licensed repositories
remain complete local/self-hosted products. Dependency direction is one way:

```text
Browser -> Service API/BFF -> hosted Agent worker -> public D&D MCP facade
                           -> PostgreSQL (cloud workflow/projections/usage)
                           -> private S3/MinIO (Pack archives)

D&D MCP -> sagasmith-dnd -> sagasmith-core
Agent   -> D&D Skills and module-generation Skills
```

The Service never opens the D&D MCP database. Campaign state, phase, random streams, revisions,
idempotency, snapshots, branches, undo/redo, actor scope, settlement, and Pack activation remain
MCP-owned. Service records a receipt and a disposable projection after a successful public tool
call.

## Trust and identity

- Browser authentication is an opaque, hashed, revocable server session in an HttpOnly cookie.
- A stable MCP principal is derived as `user:<service-user-uuid>`. Browsers cannot submit it.
- Campaign ownership and lobby workflow are Service concepts; effective membership and actor
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

The browser does not receive or imitate MCP tools. It talks to an Agent conversation endpoint. A
hosted Agent worker owns a real MCP client session, consumes the server's current native
`tools/list`, listens for `tools/list_changed`, and refreshes schemas before the next call. Lobby,
Play, Combat and checkout/restore transitions therefore change the real native tool list.

Workers are isolated per active campaign conversation. The private Agent Supervisor starts a
dedicated Nanobot subprocess for each conversation, giving each one a separate MCP client and tool
registry without mounting the Docker socket. The cloud topology is:

```text
conversation lease -> dedicated Agent worker -> dedicated MCP session
                   -> persisted Agent workspace/session volume
                   -> idle timeout -> graceful close -> resumable lease
```

The Service-injected context contains the authenticated campaign and principal. It is a semantic
aid only; every MCP call remains fail-closed on membership, actor, phase, revision and payload.
The Supervisor also requires the `campaign:user:conversation` key to match that principal, and the
hosted worker uses the Service user UUID as Nanobot's sender identity. Internal MCP DNS names are
resolved to exact host CIDRs in an ephemeral mode-0600 worker config; no broad private-network SSRF
exception is granted.

## Data ownership

| Data | Authority | Recovery source |
|---|---|---|
| users, sessions, quotas, invitations, applications | Service PostgreSQL | PostgreSQL backup |
| campaign/member/actor display projection | Service cache | MCP reconciliation |
| campaign world and mechanic state | D&D MCP | D&D state backup/snapshot |
| Agent conversation/run and usage receipt | Service + Agent workspace | both backups |
| private Pack archive | private object storage | versioned object backup |
| imported/activated Pack state | D&D MCP | MCP backup + immutable archive |

## Pack lifecycle and copyright

The hosted library accepts finalized SagaSmith Pack archives, not commercial PDFs. Upload requires
an explicit right-to-store attestation, is always `distribution=private`, streams through a size
limit, receives a SHA-256 digest, and has no public download route. Import materializes a short-lived
copy into a volume shared only with D&D MCP, then calls `content_pack(import)` in Lobby. Import
receipts supply the runtime reference; activation is a distinct idempotent `content_pack(activate)`
call. Service changes its projection to `activated` only after that authoritative receipt.

Source authoring remains `draft -> Agent evidence review -> finalize`. Draft source, extracted text,
chunks and embeddings stay in private storage. A finalized version is immutable; edits create a new
version. Publication or cross-user sharing is a separate future policy and must reject commercial
content by default.

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
