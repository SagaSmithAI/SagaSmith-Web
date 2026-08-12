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
| Pack | private upload, byte limit, checksum, ownership isolation, MCP validation/import, immutable version |
| quota | no grant, concurrent reservations, settle/release, provider retry, exact usage ledger, admin grant audit |
| recovery | fresh migration; encrypted backup; checksum validation; isolated full restore; reconciliation |
| security | principal spoof attempt, IDOR, CSRF, rate limits, malicious archive, path traversal, secret scan |

Unit/API coverage in this repository is the fast gate. The dynamic-tool, phase, restore and native
call rows require the real hosted Agent plus D&D MCP and cannot be replaced with fabricated tool
results.
