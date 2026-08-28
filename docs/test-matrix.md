# Acceptance matrix

| Area | Required evidence |
|---|---|
| account | register/current legal acceptance/login/logout; duplicate email; revoked/expired session; profile and password change; session inventory/revoke-others; non-admin deactivation; admin separation; desktop/mobile Chromium flow |
| admission | owner campaign creation; idempotent retry; application approve/reject; invite expire/revoke/limit |
| authority | MCP receipt before projection; player cannot review; actor bind/revoke; stale revision rejected |
| room + Agent | shared ordered timeline; audience filtering; authenticated sender scope; per-principal session; chat without Agent; action quota settlement; retry same payload; payload mismatch conflict |
| async DB diagnosis | concurrent room action retry, Agent message, projection refresh and activity callback; async-engine lifecycle and rollback; HTTP success rate; event-loop lag; `async_driver` versus sync DB execution context; SQLite baseline and disposable PostgreSQL staging run |
| synchronized panels | SSE message and `state.changed`; Character/Play/Combat/Module projection refresh; player intents; DM phase/combat commands; stream reconnect recovery |
| live-room UI | private character-card scope; character/spells/inventory/party drawer; inspected versus acting actor; Grid-only map; token hover-safe fields; target/destination action context; expanded Grid preserves the single composer |
| dynamic tools | real host lookup for D&D and CoC; system-directed server selection; Lobby/Play/Combat list changes; `tools/list_changed`; next legal native call |
| phases | Lobby -> Play -> Combat -> Play; grid and Agent spatial modes; chase/combat exclusivity |
| continuity | restart/resume; snapshot/branch restore; undo/redo; exposure immediately recoverable |
| Pack | current `.sagasmith-pack` only; ZIP safety, byte/uncompressed limits, checksum, ownership isolation, MCP validation/import/activation, immutable version |
| Forge | every artifact type; immutable version; provenance/license; Agent review; moderation; search/favorite/Fork; discussion/spoiler; report/withdraw |
| public install | cross-account Module/Rule/Character install through real MCP; separate activation; Soul/Skill/Asset library reference; withdrawn release denied |
| Identity | published Soul pin; invitation/acceptance; `agent:` MCP principal; quota payer; memory namespace/revision; Agent context; authority-first revoke |
| Module Studio | hidden authoring campaign; source limits/rights; outline approval; Module Gen Skill lookup; persistent lease/recovery/cancel/retry; strict Agent decisions; real `module_draft` evidence/edit/finalize; revision and idempotency; project quota; SSE/notification |
| Module delivery | immutable compiled artifact; direct and published cross-campaign import; optional activation; version collision; restart/resume; source publication boundary |
| quota | no grant, concurrent reservations, lease renewal, active-job expiry protection, abandoned expiry, settle/release, provider retry, exact usage ledger, admin grant audit |
| durable room turns | same/different idempotency keys, stale revision, queued cancellation, expired worker lease/startup recovery, Agent result saved before projection retry, Web restart, timeout polling, terminal result references |
| MCP media boundary | standard text/image/audio/resource/embedded-resource result retention, HostMediaEnvelope projection, private artifact authorization, checksum and size rejection |
| recovery | fresh migration; encrypted destination; checksum validation; isolated full restore; restored object re-import through MCP; reconciliation |
| security | principal spoof attempt, IDOR, CSRF, rate limits, malicious archive, path traversal, secret scan |

Unit/API coverage in this repository is the fast gate. The dynamic-tool, phase, restore and native
call rows require the real hosted Agent plus both system MCPs and cannot be replaced with fabricated tool
results.

The account browser smoke is hermetic: CI starts the test-mode FastAPI app with a temporary SQLite
database and uses headless Chromium to exercise the shipped HTML, JavaScript, cookies, legal pages,
password rotation, responsive account layout, and deactivation. It does not use a production host,
real account, external model, or domain MCP.

The container acceptance provider is deterministic but the host is real: it opens exposure as a
authenticated principal, selects only the MCP matching the campaign `system_id`, searches and
selects `character_query`, observes `tools/list_changed`, and calls the newly registered native
tool in both D&D and CoC. The same run imports and activates a structurally valid, synthetic,
Agent-finalized D&D module Pack through MinIO and the public MCP facade.

With an isolated D&D MCP running, execute
`uv run python scripts/real_mcp_smoke.py --url http://127.0.0.1:8767/mcp` to prove campaign creation
and authoritative phase read through the real public facade.
