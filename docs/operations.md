# Operations and recovery

## Single-server installation

The server checks out this private repository beside the required open repositories. Run
`install.bat` on Windows or copy `.env.example` and `config/agent-config.example.json` manually.
Replace every `replace-*` value, set the model credential, then run `start.bat` or
`docker compose up -d --build`.

The five `SAGASMITH_*_CONTEXT` values select the open-source build inputs. Pin reviewed tags or
commit SHAs for production; never deploy moving branch references. Remote Git contexts deliberately
avoid sending unrelated local worktrees, virtual environments or private content to Docker.

The private stack contains Caddy, Service API/Web, PostgreSQL, Redis, MinIO, D&D MCP and Agent.
Only ports 80/443 are public. Service starts with `alembic upgrade head`. For a real hostname set
`SAGASMITH_SITE_ADDRESS` to the hostname and `SAGASMITH_SECURE_COOKIES=true`.

## Health and observability

- `/api/health`: process liveness.
- `/api/ready`: PostgreSQL readiness.
- `/metrics`: Prometheus counters and latency histograms; firewall it in production.
- `X-Request-ID`: accepted only in a safe shape or generated, echoed, and logged.
- Alert on readiness failures, 5xx rate, Agent/D&D MCP failures, p95 latency, quota settlement lag,
  moderation queue age, copyright-report age, disk/object capacity and backup age.

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

`backup.bat` creates a timestamped folder containing a PostgreSQL custom dump and compressed copies
of private object storage, D&D state and Agent workspaces, plus SHA-256 checksums. Copy the completed
folder to encrypted off-host storage. Redis is a queue/cache and is not a recovery authority.
The script stops all application writers for a consistent cut, records the Service commit and dirty
state, verifies the finished manifest, checks every native Docker/Git exit code, and only then
restarts healthy services. The destination filesystem itself must provide encryption at rest; the
backup folder is deliberately portable and is not encrypted by the script. The manifest records
the Compose inputs and exact container image IDs. Secrets and deployment configuration are not
copied into the data backup; escrow them separately in a secrets manager with its own recovery
procedure.

Recommended policy: daily backups retained 30 days, weekly retained 12 weeks, monthly retained one
year. Object versioning is additional protection, not a substitute for a separate backup.

## Restore drill

1. Provision a clean isolated host with the exact tagged open-source and Service releases.
2. Verify every file against `manifest.json` before extracting.
3. Restore object, D&D-state and Agent-workspace volumes while their services are stopped.
4. Start PostgreSQL, restore `control.dump` with `pg_restore --clean --if-exists`, then run migrations.
5. Start D&D MCP and reconcile campaign/member/actor/Pack projections from public reads.
6. Start one isolated Agent worker, resume a test conversation, refresh native tools, and execute the
   next legal Lobby call.
7. Verify Lobby -> Play -> Combat -> Play, snapshot/branch restore, undo/redo, quota idempotency and
   private Pack access before reopening traffic.

`restore.bat` refuses the live project name, requires `RESTORE-<project>` confirmation, requires a
fresh project with no existing volumes, verifies hashes before extraction, and never starts the
proxy. The automated smoke waits for external readiness, logs in, checks control-plane/audit state,
reads authoritative campaign state, then downloads a restored private Pack from object storage and
imports it through the restored D&D MCP. Never test destructive restore commands against the live
volumes. Quarterly drills should record RPO, RTO, release hashes, backup ids and discrepancies.
