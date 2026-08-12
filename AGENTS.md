# SagaSmith Service Development Contract

This is the closed-source hosted SagaSmith product. The sibling open-source repositories remain
complete local/self-hosted products and must never depend on this repository.

## Authority boundaries

- This service owns accounts, sessions, plans, quota reservations, usage, billing, invitations,
  join-request workflow, notifications, hosted process orchestration, and cloud projections.
- `SagaSmith-dnd-mcp` remains the only authority for campaign membership, actor authority,
  revisions, idempotency, random streams, atomic settlement, phases, and Pack activation.
- `sagasmith-dnd` owns deterministic D&D mechanics. `sagasmith-core` owns system-neutral
  persistence, documents, retrieval, versions, and transactions.
- The service may cache projections but must not write open-source game tables directly.
- Agent and Workbench clients must use the server-owned dynamic MCP tool list. Never introduce a
  fixed tool superset, text imitation, compatibility alias, or fallback protocol.

## Product boundaries

- Keep cloud account, quota, billing, lobby, and administration code in this repository.
- Consume tagged/reproducible open-source releases through adapters under `integrations/`.
- Commercial source files, extracted text, chunks, embeddings, Drafts, and Packs are private.
- Pack publication remains `draft -> Agent review -> finalize`; activation is a separate Lobby
  operation.
- Browser requests never choose an authoritative principal. The service derives `user:<uuid>`
  from a server-side session and injects it into trusted internal calls.

## Completion evidence

- Test public APIs and real integration adapters, not internal service methods alone.
- Cover registration/login, ownership, join approval, MCP receipt reconciliation, actor binding,
  quota reserve/settle/release, retry/idempotency, permission revocation, Pack privacy, restart,
  backup, and restore.
- Keep secrets out of source, logs, fixtures, container images, and browser bundles.
