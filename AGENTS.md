# SagaSmith Web Development Contract

This repository currently keeps the historical name `SagaSmith-service`, but the product is
**SagaSmith Web**: browser frontend, API/BFF, hosted control plane, collaboration, Forge, Module
Studio, Agent orchestration, and operations. The Python package remains `sagasmith_service` and
the existing CLI names remain stable until a separately reviewed migration.

The sibling open-source repositories form the complete SagaSmith Local Agent Kit and must never
depend on this repository. Local and hosted deployments must cross the same authoritative domain
MCP contract. They may differ in transport, authentication, storage, and deployment, but not in
tool schemas, rule semantics, authoritative writes, revision behavior, or idempotency.

## Current source topology

- `sagasmith-dnd`, `sagasmith-coc`, and `sagasmith-narrative` are the only current
  domain source inputs. Their repository-local `packages/mcp`, `skills`, and UI
  paths replace the former standalone component repositories.
- `sagasmith-core` remains an independent neutral dependency and `SagaSmith-agent`
  remains the host/orchestration dependency.
- Former standalone MCP, Skills, UI, and generic Module Generator repositories
  are archived read-only. Never pin, clone, build, or document them as a fallback.
- `component-versions.json` and `compose.yaml` must agree on every enforced source
  revision. Remove archived components from the current lock instead of carrying
  them as legacy entries.

## Authority boundaries

- SagaSmith Web owns accounts, sessions, plans, quota reservations, usage, billing, invitations,
  join-request workflow, notifications, hosted process orchestration, and cloud projections.
- The matching MCP inside `sagasmith-dnd`, `sagasmith-coc`, or `sagasmith-narrative` remains the
  only authority for campaign membership, actor authority, revisions, idempotency, random streams,
  atomic settlement, phases, and Pack activation.
- Each domain monorepo owns its deterministic mechanics, MCP, Skills, and domain UI where present.
  `sagasmith-core` owns system-neutral persistence, documents, retrieval, versions, and transactions.
- SagaSmith Web may cache projections but must not write open-source game tables directly.
- Agent and Workbench clients must use the server-owned dynamic MCP tool list. Never introduce a
  fixed tool superset, text imitation, compatibility alias, or fallback protocol.

## Product boundaries

- Keep cloud account, quota, billing, campaign admission, room messaging, and administration code
  in this repository. `lobby` is reserved for the D&D MCP runtime phase.
- Keep Forge catalog, release moderation, discussion, hosted Soul/Identity profiles, assignments,
  and campaign-isolated Identity memory in this repository. Never copy live actor or campaign
  authority into a community artifact.
- Consume tagged/reproducible open-source releases through adapters under `integrations/`.
- Commercial source files, extracted text, chunks, embeddings, Drafts, and Packs are private.
- Pack publication remains `draft -> Agent review -> finalize`; activation is a separate Lobby
  operation.
- Browser requests never choose an authoritative principal. The service derives `user:<uuid>`
  from a server-side session and injects it into trusted internal calls.
- Hosted Identity calls derive `agent:<identity-uuid>` only from an accepted campaign assignment;
  Soul instructions and memory cannot weaken MCP authorization or dynamic tool exposure.
- Public releases reject private/commercial source and executable rules. Published releases are
  immutable, and report-driven withdrawal preserves audit evidence.

## Completion evidence

- Test public APIs and real integration adapters, not internal service methods alone.
- Cover registration/login, ownership, join approval, MCP receipt reconciliation, actor binding,
  quota reserve/settle/release, retry/idempotency, permission revocation, Pack privacy, restart,
  backup, and restore.
- Keep secrets out of source, logs, fixtures, container images, and browser bundles.
