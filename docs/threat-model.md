# Threat model

## Protected assets

Private campaign state, DM-only narration, actor-private data, account sessions, model credentials,
quota, commercial Pack archives, extracted source content, backups, and audit trails are sensitive.

## Controls by threat

| Threat | Control |
|---|---|
| credential theft | Argon2 password hashes; opaque random sessions; only token hashes stored; HttpOnly/Secure/SameSite cookies |
| principal spoofing | principal derived from server session; never accepted from browser; Supervisor binds it to the conversation key; hosted worker maps it to the real sender; MCP re-authorizes each call |
| cross-campaign access | membership lookup in BFF plus authoritative MCP campaign/actor checks; conversation ids are owner-scoped |
| CSRF | SameSite cookie plus exact scheme/host Origin enforcement on every authenticated mutating request, including Pack multipart |
| quota race or retry double-charge | grant rows locked; reservation before call; unique idempotency keys; append-only usage ledger |
| invite theft | 256-bit random value shown once; only SHA-256 stored; expiry, revoke and use counter |
| malicious upload | current `.sagasmith-pack` envelope only; compressed/uncompressed/entry/ratio limits, restricted paths, private bucket, checksum, rights attestation; MCP validates finalized archive before import |
| public copyright laundering | explicit provenance/license/rights attestation; real Agent review; administrator moderation; private-source publication block; report queue can withdraw releases without deleting audit evidence |
| executable community rules | Rule manifest/payload rejects executable/script/binary entry shapes; production executes only reviewed open-source system/MCP code |
| stored XSS from community text | JSON APIs plus browser `textContent`; restrictive CSP; no user HTML rendering |
| Identity privilege escalation | campaign owner invitation plus Identity-owner acceptance; Service-derived `agent:<uuid>`; MCP grant before assignment activation; authority-first revoke |
| cross-campaign Identity memory | unique assignment namespace, assignment FK, DM/Identity-owner checks, revision conflict, no memory copy into Soul/public profile |
| path traversal | server-generated object keys; resolved paths constrained to storage/exchange roots |
| SSRF | MCP and Agent URLs are deployment configuration, never user input; worker grants only exact DNS-resolved MCP host CIDRs, not a private-network wildcard |
| prompt/tool injection | Agent is semantic authority but cannot bypass native schemas or MCP authorization/revision checks |
| stale dynamic tools | real MCP session listens to `tools/list_changed`; call-time MCP checks remain final |
| service compromise | containers run non-root, only proxy is public, databases and MCP are internal, secrets are mounted/env and excluded from Git |
| ransomware/operator error | timestamped PostgreSQL/object/D&D/Agent backups, off-host encrypted copy, restore drills |

## Required production hardening

Set secure cookies and a real HTTPS hostname, rotate all example secrets, restrict `/metrics` to the
monitoring network, retain the application rate limits and add edge abuse controls, configure object encryption
and versioning, send immutable audit copies off-host, scan images and dependencies, and do not mount
the Docker socket into API or Agent containers.

Commercial PDFs, their extracted text and generated embeddings must never enter Git, public object
storage, telemetry payloads, error reports or shared logs.

Resolving a copyright, commercial-source, privacy, or malware report withdraws the public Artifact
and every published Release immediately. Existing installation/audit rows remain for incident
response; no new install can pass the published-release check.
