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
the domain MCP revalidates revision and authority on every call. Agent and independent projection
work runs without holding the room or database lock. Only the final ordered message/outbox write is
serialized by a short per-room settlement lock, so different rooms and independent reads remain
concurrent.

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
returns standard MCP content blocks; the Host saves that result unchanged, then converts media and
resources into `sagasmith.host-media/v1` object references protected by room audience checks.

## Deployment and rollback

Run `alembic upgrade head` before enabling workers. During a rolling deployment, old replicas must
be drained before the migration because they do not understand `room_turn_jobs`. To roll back the
application, first stop action admission, wait for active jobs to become terminal, retain a database
and object-store backup, and then deploy the previous application. Do not run the migration
downgrade while jobs or media artifacts must be retained: downgrade removes the new orchestration
tables. Replaying a failed publication with the same idempotency key is safer than deleting rows.
