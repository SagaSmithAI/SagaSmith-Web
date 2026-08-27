# Async database hot-path diagnosis

SagaSmith Web deliberately keeps low-frequency administration, community, and CRUD endpoints on
the synchronous SQLAlchemy engine. Before introducing a second engine/session stack, measure the
four paths where database work and awaited upstream work can overlap:

| Operation class | Route shape | Current execution model |
|---|---|---|
| `agent_message` | `POST /api/campaigns/{id}/agent/conversations/{id}/messages` | async handler, sync session |
| `room_action` | `POST /api/campaigns/{id}/room/messages` and panel intents | async handler, sync session |
| `projection_refresh` | `GET /api/campaigns/{id}/room/panel` | async handler, sync session |
| `activity_callback` | `POST /api/campaigns/{id}/room/internal-activity/{id}` | sync handler in worker thread |

The callback is a useful control: it uses the same sync engine but does not execute database calls
on the event loop. The `room_action` route label also observes chat and narration posted through
the same `/room/messages` endpoint; the benchmark payload is always `mode=action`, and HTTP route
metrics can separate the mixed production route volume from the benchmark result.

## Metrics

The API exports these series on `/metrics`:

- `sagasmith_event_loop_lag_seconds`: maximum scheduling drift during one selected request;
- `sagasmith_db_statement_seconds`: cursor latency split by statement class and execution context;
- `sagasmith_db_request_seconds`: summed cursor latency per request and execution context;
- `sagasmith_db_statements_per_request`: statement count per request and execution context.

Labels are restricted to the four operation classes, `select|insert|update|delete|transaction|other`,
`event_loop|worker`, and `success|error`. They never contain a user, campaign, room, run, tool,
query text, or argument. Cursor timing does not include ORM object construction and therefore must
be interpreted together with route latency and event-loop lag.

## Reproducible harness

The harness creates one isolated account, campaign, room, conversation, and activity run per
concurrency lane. Lanes are independent so shared campaign sequence locks do not manufacture
contention. A deterministic in-process Agent/domain runtime isolates SagaSmith Web and database
latency while retaining the public HTTP routes, authentication, quota, idempotency, projection,
and activity contracts.

Run the temporary-file SQLite baseline:

```powershell
uv run python scripts/benchmark_async_hotpaths.py `
  --concurrency 4 --iterations 5 --upstream-delay-ms 5 --lag-interval-ms 2
```

Run one route family when investigating a change:

```powershell
uv run python scripts/benchmark_async_hotpaths.py `
  --scenario room_action --concurrency 8 --iterations 20
```

For PostgreSQL, create a dedicated disposable database and pass its URL explicitly. The harness
creates persistent benchmark rows and will reject any explicit URL unless that write is
acknowledged (the no-URL default remains an automatically removed SQLite file):

```powershell
uv run python scripts/benchmark_async_hotpaths.py `
  --database-url 'postgresql+psycopg://user:password@host:5432/disposable_benchmark' `
  --allow-database-writes --concurrency 16 --iterations 50
```

Never point the harness at production or a database containing campaign data. Delete the dedicated
database through the normal database administration process after retaining the anonymized JSON
summary.

`--simulated-db-delay-ms` exists only to calibrate that the probe detects an injected stall. An
injected delay is never evidence for a migration decision.

## 2026-08-27 SQLite baseline

Baseline commit: `888311c` plus this instrumentation and harness. Environment: Windows, CPython
3.12, temporary file-backed SQLite, four lanes, five measured iterations per lane, 5 ms synthetic
upstream await, and a 2 ms lag sampling interval. No database delay was injected.

| Operation | Success | p95 request | Max loop lag | Event-loop DB share |
|---|---:|---:|---:|---:|
| agent message | 20/20 | 94.95 ms | 14 ms | 30.55% |
| room action | 8/20 | 16.84 s | 11.19 s | 99.98% |
| projection refresh | 20/20 | 25.71 ms | 14 ms | 35.75% |
| activity callback control | 20/20 | 112.01 ms | 14 ms | 0% |

All 12 room-action failures were HTTP 500 responses caused by SQLite `database is locked` errors.
The failed cursor calls spent about 67 seconds waiting on event-loop-executed inserts and updates.
This is strong evidence that a synchronous lock wait in the async room handler can prevent another
coroutine from reaching the commit that would release the lock. The worker-thread callback also
serialized SQLite writes, but completed every request and attributed no database time to the event
loop.

The Docker client was installed, but its daemon was unavailable during this baseline, so no
PostgreSQL number is claimed. SQLite proves a real local deployment failure and validates the
diagnostic route; it is not a substitute for staging PostgreSQL evidence.

## Migration gate

Run at least three staging passes with representative PostgreSQL latency and one current worker
configuration. Preserve status counts; p95 from only successful requests is insufficient. Migrate
the smallest path when any of these repeat:

1. a database timeout, lock failure, or HTTP 5xx occurs under supported concurrency;
2. hot-path p95 event-loop lag exceeds 25 ms and is at least twice the callback/control lag;
3. event-loop sync cursor time exceeds 20% of request wall time or one statement class repeatedly
   exceeds 5 ms p95;
4. increasing worker/replica count moves the bottleneck to PostgreSQL without restoring the route
   latency objective.

Start with `room_action`; the SQLite failure already makes it the first candidate. Before changing
its session type, explicitly map transaction boundaries around Agent completion, quota settlement,
activity callbacks, MCP projection reads, idempotency, and the final room sequence allocation.
Do not hold a database transaction across an Agent or MCP await. Validate concurrent retries,
payload-mismatch conflicts, quota settlement, callback sequence monotonicity, and authority-first
receipts against both SQLite and PostgreSQL.

`agent_message` and `projection_refresh` should move only if their own staging measurements cross
the gate. `activity_callback` should remain synchronous unless its worker-pool saturation, rather
than event-loop lag, is independently demonstrated. Low-frequency admin/community CRUD remains on
the sync engine.
