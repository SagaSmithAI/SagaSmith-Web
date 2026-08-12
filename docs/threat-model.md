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
