# Operations and recovery

## Component release gate

The default Compose build uses immutable reviewed SagaSmith revisions. Before changing any
component context, update `component-versions.json`, review the upstream protocol changes, and run:

```powershell
uv run python scripts/audit_components.py --fetch --strict
uv run python scripts/audit_components.py --scope build --strict --json
docker compose config
```

During coordinated local development, use
`docker compose -f compose.yaml -f compose.workspace.yaml up --build` so the image consumes the
current sibling worktrees. This override is not a release input and must not be used to replace the
immutable production component lock.

Do not replace the pinned defaults with moving branches in a production release. Environment
overrides are intended for an explicit candidate revision during review, after which the accepted
SHA becomes the new lock.

The Agent supervisor intentionally has one SagaSmith Web-owned integrated dependency lock at
`infrastructure/agent-supervisor-requirements.txt`. Agent's standalone `uv.lock` remains
authoritative for standalone Nanobot installations, while the hosted image must reconcile Agent
`[api]` and SagaSmith Web constraints as one environment. Regenerate it with
`uv run python scripts/lock_agent_supervisor.py` whenever either `pyproject.toml` changes; do not
install two independently pinned locks into one Python environment. The generator deliberately
omits dependency-source annotations so the committed hash lock is reproducible and never records a
developer machine's absolute Agent checkout path. It also resolves through the exact uv version and
dependency cutoff declared in `component-versions.json`, so later package releases cannot silently
change a regeneration.

## Tagged release procedure

SagaSmith Web releases use `v<project.version>` tags on commits already contained in `main`.
The release workflow refuses a mismatched version, a non-main commit, a dirty candidate, a missing
release input, or a component lock that fails strict remote verification. A successful release
publishes the Python wheel and source distribution, SHA-256 checksums, a machine-readable release
manifest, and two immutable GHCR image references (semantic version and commit SHA). GitHub records
build provenance for the Python artifacts and container, while BuildKit attaches a software bill
of materials and maximum-mode provenance to the registry image.

Before creating the tag, merge the version bump and current component lock through the protected
branch, confirm the required CI and CodeQL checks, and review the generated dependency lock. Create
an annotated tag from the clean `main` checkout; never reuse or move a published tag. The workflow
builds release artifacts from the tagged commit and does not deploy them to a live environment.

Verify downloaded Python artifacts with `SHA256SUMS`, verify their GitHub attestation, and deploy
the container by digest recorded in `release-manifest.env`, not by a mutable tag. Keep environment
secrets and deployment-specific configuration outside every release asset.

The local `--fetch --strict` audit diagnoses sibling-worktree drift. The release workflow instead
uses `--remote --scope build --strict`, which fetches commit history without blobs into temporary
bare repositories and verifies that every enforced locked SHA is an ancestor of its declared
remote branch. This keeps release evidence independent of developer checkout state.

## Single-server installation

The server checks out this private repository beside the required open repositories. Copy
`.env.example` to `.env` and `config/agent-config.example.json` to
`secrets/agent-config.json`, then replace every `replace-*` value and set the model credential.
Start the hosted stack with `docker compose up -d --build`; inspect it with
`docker compose ps` and stop it with `docker compose down`.

The six `SAGASMITH_*_CONTEXT` values select the open-source build inputs, including
`SAGASMITH_MODULE_GEN_SKILLS_CONTEXT`. Pin reviewed tags or
commit SHAs for production; never deploy moving branch references. Remote Git contexts deliberately
avoid sending unrelated local worktrees, virtual environments or private content to Docker.

The private stack contains Caddy, SagaSmith Web API/frontend, persistent Module worker, PostgreSQL, Redis,
MinIO, D&D MCP, CoC MCP and the Agent Supervisor. Narrative remains process-local to each Hosted
Worker and is probed through the Supervisor rather than exposed as a network service.
Only ports 80/443 are public. SagaSmith Web starts with `alembic upgrade head`. For a real hostname set
`SAGASMITH_SITE_ADDRESS` to the hostname and `SAGASMITH_SECURE_COOKIES=true`.

The Supervisor keeps one isolated process per conversation, starts different conversations with
bounded parallelism, and coalesces concurrent starts for the same conversation. Size
`SAGASMITH_AGENT_MAX_WORKERS` to the host memory budget, keep
`SAGASMITH_AGENT_SPAWN_CONCURRENCY` low enough to avoid a cold-start CPU spike, and tune
`SAGASMITH_AGENT_IDLE_SECONDS` for the desired warm-worker/LRU tradeoff. When capacity is full and
all workers are serving requests, new cold conversations receive HTTP 503 instead of creating
unbounded processes; clients should retry with backoff.

## Health and observability

- `/api/health`: process liveness.
- `/api/ready`: readiness for PostgreSQL, D&D, CoC and Narrative runtimes, Agent, rate limiter,
  and private object storage. Any missing dependency returns HTTP 503 with a
  per-component status map.
- `/metrics`: Prometheus counters and latency histograms; firewall it in production.
- Outbound latency is split across `sagasmith_agent_upstream_seconds`,
  `sagasmith_mcp_transport_setup_seconds`, `sagasmith_mcp_initialize_seconds`,
  `sagasmith_mcp_exposure_seconds`, and `sagasmith_mcp_tool_seconds`. Room fan-out is measured by
  `sagasmith_room_projection_batch_seconds` and `sagasmith_room_projection_jobs`. These series use
  only bounded `system`, `operation_class`, `status`, and `transport` labels; never add campaign,
  user, run, or tool-argument values.
- Durable room orchestration exposes `sagasmith_room_turn_job_transitions_total`,
  `sagasmith_room_turn_job_seconds`, `sagasmith_room_turn_job_recoveries_total`, and
  `sagasmith_room_turn_job_queue`. Labels are bounded state/phase/reason classes; job, room,
  campaign, user, prompt, and tool arguments are never metric labels.
- Selective database diagnosis uses `sagasmith_event_loop_lag_seconds`,
  `sagasmith_db_statement_seconds`, `sagasmith_db_request_seconds`, and
  `sagasmith_db_statements_per_request` for room actions, Agent messages, projection refreshes, and
  activity callbacks. Labels are bounded operation/statement/execution/status classes;
  `async_driver` distinguishes awaited database I/O from blocking sync-driver calls on the event
  loop. Room messages use async authentication and persistence; panel intents use async
  persistence after sync authentication; projection refreshes use async authentication and end
  their local read transaction before MCP. Agent messages and authoritative panel commands remain
  evidence-gated, while activity callbacks stay in worker threads. Follow the
  [async database hot-path runbook](async-database-hotpaths.md) and never benchmark against
  production campaign data.
- Realtime delivery exposes `sagasmith_realtime_subscriptions`,
  `sagasmith_realtime_db_queries_total`, `sagasmith_realtime_wakeups_total`,
  `sagasmith_outbox_deliveries_total`, and `sagasmith_outbox_pending`. Room and Module SSE use
  Redis wake-ups plus PostgreSQL cursor replay; a 30-second database reconciliation is the
  missed-event safety net, not the normal update loop. Alert when pending outbox rows grow or
  delivery errors persist. These metrics use only bounded stream/reason/status labels.
- `module-worker:9101/metrics`: Module task outcomes and expired-lease recovery counters on the
  private network.
- `agent:8910/metrics`: Agent worker spawn latency, bounded spawn queue, ready/busy/spawning/
  retiring worker counts, capacity rejections, and aggregate tracked-worker RSS. The endpoint is
  private-network only and is included in the observability profile's Prometheus scrape targets.
- `X-Request-ID`: accepted only in a safe shape or generated, echoed, and logged.
- Alert on readiness failures, 5xx rate, Agent/D&D MCP failures, p95 latency, quota settlement lag,
  moderation queue age, copyright-report age, disk/object capacity and backup age.
- Also alert on queued Module tasks older than five minutes, repeated lease recovery, terminal
  task failures, project budget exhaustion and Agent/MCP idempotency conflicts.

The optional production observation profile is pinned separately from the application stack:

```powershell
docker compose -f compose.yaml -f compose.observability.yaml --profile observability up -d
```

It adds Prometheus, Grafana, Loki, Tempo, an OTLP Collector and Alloy log shipping. All dashboards
and ingestion ports bind to loopback by default; set `GRAFANA_ADMIN_PASSWORD` before any real
deployment and place authentication/TLS in front of them before changing those bindings. The
profile retains 30 days of local metrics, logs and traces. Alloy mounts the Docker socket read-only,
discovers only the Compose project named by `SAGASMITH_OBSERVABILITY_PROJECT` (default
`sagasmith-service`), and labels every stream by Compose project and service. Set that variable to
the value passed with `docker compose -p` when overriding the project name. This requires a Linux
Docker Engine or Docker Desktop's Linux VM; use an equivalent scoped log source with another
container runtime.

NATS/JetStream is deliberately absent: no current SagaSmith component consumes it, and adding a
second unused queue would create split authority with PostgreSQL task leases and Redis rate/session
coordination. Introduce it only with a concrete durable event contract and migration plan.

## Forge moderation

Only administrators can approve a release or resolve a report. Review the rights statement,
license, provenance, Agent findings, Pack validation and any suspicious overlap before approval.
Agent approval is evidence, not a legal determination. Treat copyright, commercial-source,
privacy, and malware reports as urgent; resolving one withdraws the Artifact and all published
releases. Never "repair" a questionable commercial upload into a public Pack. Preserve the audit
trail and contact the uploader outside the public discussion when necessary.

DM/Keeper Identities must use a published Soul and explicit availability. Monitor orphaned pending
invitations and revoked assignments. Campaign memory is part of the PostgreSQL backup and must be
handled as DM-private campaign data, not community content.

## Backup

`powershell -NoProfile -File scripts/backup.ps1` creates a timestamped folder containing a
PostgreSQL custom dump and compressed copies
of private object storage, D&D/CoC state and Agent workspaces, plus SHA-256 checksums. Copy the completed
folder to encrypted off-host storage. Redis is a queue/cache and is not a recovery authority.
The script stops all application writers for a consistent cut, records the SagaSmith Web commit and dirty
state, verifies the finished manifest, checks every native Docker/Git exit code, and only then
restarts healthy services. The destination filesystem itself must provide encryption at rest; the
backup folder is deliberately portable and is not encrypted by the script. The manifest records
the Compose inputs and exact container image IDs. Secrets and deployment configuration are not
copied into the data backup; escrow them separately in a secrets manager with its own recovery
procedure.

Recommended policy: daily backups retained 30 days, weekly retained 12 weeks, monthly retained one
year. Object versioning is additional protection, not a substitute for a separate backup.

`.github/workflows/nightly-recovery.yml` now seeds all three hosted domains, creates and verifies an
application-consistent backup, restores it under a distinct Compose project and host ports, and
exercises the restored control database, object storage and domain state. Both projects and their
volumes are removed in the unconditional cleanup step.

The normal container acceptance also stops Redis and requires protected requests to fail closed,
restarts D&D/CoC MCP while a Worker has a live session, restarts the Agent before resuming a
Narrative conversation, and waits for every idle Worker to disappear with no `/proc` orphan.

## Restore drill

1. Provision a clean isolated host with the exact tagged open-source and SagaSmith Web releases.
2. Verify every file against `manifest.json` before extracting.
3. Restore object, D&D-state, CoC-state and Agent-workspace volumes while their services are stopped.
4. Start PostgreSQL, restore `control.dump` with `pg_restore --clean --if-exists`, then run migrations.
5. Start both system MCPs and reconcile campaign/member/actor/Pack projections from audience-safe reads.
6. Start one isolated Agent worker, resume a test conversation, refresh native tools, and execute the
   next legal Lobby call.
7. Resume a queued Module task, verify its current MCP draft revision, compile it and install the
   content artifact into a disposable campaign.
8. Verify Lobby -> Play -> Combat -> Play, snapshot/branch restore, undo/redo, quota idempotency and
   private Pack access before reopening traffic.
9. Before admitting writes, verify that expired `running`/`waiting` room-turn leases were recovered,
   active reservation heartbeats are advancing, and a queued smoke action reaches one terminal
   state without a duplicate Agent request.

`powershell -NoProfile -File scripts/restore.ps1` refuses the live project name, requires
`RESTORE-<project>` confirmation, requires a
fresh project with no existing volumes, verifies hashes before extraction, and never starts the
proxy. The automated smoke waits for external readiness, logs in, checks control-plane/audit state,
reads authoritative campaign state, then downloads a restored private Pack from object storage and
imports it through the restored D&D MCP. Never test destructive restore commands against the live
volumes. Quarterly drills should record RPO, RTO, release hashes, backup ids and discrepancies.
