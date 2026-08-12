# Acceptance matrix

| Area | Required evidence |
|---|---|
| account | register/login/logout; duplicate email; revoked/expired session; admin separation |
| lobby | owner campaign creation; idempotent retry; application approve/reject; invite expire/revoke/limit |
| authority | MCP receipt before projection; player cannot review; actor bind/revoke; stale revision rejected |
| Agent | authenticated scope injection; per-conversation session; failure release; retry same payload; payload mismatch conflict |
| dynamic tools | real host lookup; Lobby/Play/Combat list changes; `tools/list_changed`; next legal native call |
| phases | Lobby -> Play -> Combat -> Play; grid and Agent spatial modes; chase/combat exclusivity |
| continuity | restart/resume; snapshot/branch restore; undo/redo; exposure immediately recoverable |
| Pack | current `.sagasmith-pack` only; ZIP safety, byte/uncompressed limits, checksum, ownership isolation, MCP validation/import/activation, immutable version |
| Forge | every artifact type; immutable version; provenance/license; Agent review; moderation; search/favorite/Fork; discussion/spoiler; report/withdraw |
| public install | cross-account Module/Rule/Character install through real MCP; separate activation; Soul/Skill/Asset library reference; withdrawn release denied |
| Identity | published Soul pin; invitation/acceptance; `agent:` MCP principal; quota payer; memory namespace/revision; Agent context; authority-first revoke |
| Module Studio | hidden authoring campaign; source limits/rights; outline approval; Module Gen Skill lookup; persistent lease/recovery/cancel/retry; strict Agent decisions; real `module_draft` evidence/edit/finalize; revision and idempotency; project quota; SSE/notification |
| Module delivery | immutable compiled artifact; direct and published cross-campaign import; optional activation; version collision; restart/resume; source publication boundary |
| quota | no grant, concurrent reservations, settle/release, provider retry, exact usage ledger, admin grant audit |
| recovery | fresh migration; encrypted destination; checksum validation; isolated full restore; restored object re-import through MCP; reconciliation |
| security | principal spoof attempt, IDOR, CSRF, rate limits, malicious archive, path traversal, secret scan |

Unit/API coverage in this repository is the fast gate. The dynamic-tool, phase, restore and native
call rows require the real hosted Agent plus D&D MCP and cannot be replaced with fabricated tool
results.

The container acceptance provider is deterministic but the host is real: it opens exposure as a
player, searches and selects `character_query`, observes `tools/list_changed`, and calls the newly
registered native tool. The same run imports and activates a structurally valid, synthetic,
Agent-finalized D&D module Pack through MinIO and the public MCP facade.

With an isolated D&D MCP running, execute
`uv run python scripts/real_mcp_smoke.py --url http://127.0.0.1:8767/mcp` to prove campaign creation
and authoritative phase read through the real public facade.
