# Durable room-turn operations

SagaSmith Web persists every action as a `RoomTurnJob`. This Web Host job is intentionally separate
from the MCP Tasks extension: it coordinates a complete LLM turn, while an MCP Task may represent
only one negotiated long-running domain tool such as a large import or high-resolution render.

## State and recovery

`queued -> running -> succeeded` is the normal path. A retryable failure releases the lease and
moves to `queued` before Agent completion or `waiting` after the standard Agent result has been
saved. Terminal states are `succeeded`, `failed`, and `cancelled`. Each claim records the worker,
attempt, heartbeat, and lease expiry. Startup and periodic recovery reclaim expired leases; retry
budgets end in a structured terminal error. `GET /api/campaigns/{campaign_id}/room/jobs/{job_id}`
is the recovery/polling surface, and cancellation is cooperative for an already-running provider
request.

The browser creates one idempotency key for the business action and reuses it for network retries.
The Web job derives stable reservation, Agent, settlement, artifact, message, and outbox keys from
that operation. Once Agent completion is saved, a projection or publication retry reuses the
stored standard `CallToolResult`; it never starts a second business operation.

## Revision and scheduling

Actions may include `base_revision`. SagaSmith Web rejects a stale projection before queueing and
prefetches the authoritative MCP phase/revision without holding a database or room settlement lock.
A short compare-and-set transaction then persists the exact, sorted system/phase/role/task facade
subset (hard limit 16); unknown phases, roles, tasks, or empty subsets fail closed. If revision
changed during selection, the job returns `stale_revision` and the client refreshes before creating
a new operation. Retries reuse the persisted subset, base revision, and idempotency key so a domain
commit whose HTTP response was lost can be recovered safely. The domain MCP revalidates revision and
authority on every call. Only the final ordered message/outbox write is serialized by a short
per-room settlement lock, so different rooms and independent reads remain concurrent.

The process-wide pool is configured by `SAGASMITH_ROOM_TURN_WORKER_CONCURRENCY`; the additional
`SAGASMITH_ROOM_TURN_PER_ROOM_CONCURRENCY` scheduler defaults to four active turns per room. Claiming
a job takes a short database row lock for its room and counts unexpired running leases, so separate
processors and Web replicas share one durable limit. The transaction commits before Agent/MCP I/O;
there is no database or settlement lock spanning a turn.

Successful authoritative changes emit a rebuildable `state.changed` receipt and transactional
outbox row containing `authority_revision`, sorted `changed_scopes`, `entity_ids`, and `audience`.
The revisioned projection cache can invalidate exactly those keys. A failed transaction emits no
row, and a successful no-op whose authority revision did not advance emits no invalidation.

## Error boundary

The durable job records a stable error code, class, retryability flag, and safe recovery hint.
HTTP polling and inline completion map conflict/cancel to 409, model-correctable request or tool
execution errors to 422, retryable upstream/storage/state failures to 503, non-retryable upstream
failures to 502, and unexpected Host failures to 500. Retrying a retryable result must preserve the
original browser idempotency key.

## Quota lease

`SAGASMITH_AGENT_RESERVATION_TTL_SECONDS` must be greater than
`SAGASMITH_AGENT_COMPLETION_TIMEOUT_SECONDS`; startup fails if the relationship is unsafe. The job
heartbeat renews the reservation. Balance calculations continue counting a reserved row after its
timestamp until an explicit reaper proves no active room or Module job owns it. Settlement occurs
immediately after Agent completion and before Web publication, allowing publication recovery with
no double charge.

## Identity, traces, and media

Jobs persist the `sagasmith.auth-context/v2` authority inputs and W3C `traceparent`, `tracestate`,
and `baggage`, but never browser tokens. Player text remains a separate untrusted input. The Agent
wire projection uses the Hosted Worker's exact trusted-context fields, exact MCP service audience,
and exact model-visible tool IDs; Web-only catalog evidence never enters that wire object. The Agent
returns standard MCP content blocks; the Host saves that result unchanged, then converts media and
resources into `sagasmith.host-media/v1` object references protected by room audience checks.

## Deployment and rollback

Run `alembic upgrade head` before enabling workers. During a rolling deployment, old replicas must
be drained before the migration because they do not understand `room_turn_jobs`. To roll back the
application, first stop action admission, wait for active jobs to become terminal, retain a database
and object-store backup, and then deploy the previous application. Do not run the migration
downgrade while jobs or media artifacts must be retained: downgrade removes the new orchestration
tables. Replaying a failed publication with the same idempotency key is safer than deleting rows.
