# DND invitation beta — preparation and acceptance

Updated 2026-09-16, Asia/Singapore. Local preparation and the six reported behavior
repairs are complete. Live launch acceptance is pending because the user explicitly
left the domain, server and DeepSeek configuration blank. No public deployment,
real paid-model run, registry publication or release tag is claimed.

## Candidate

- Web: `feat/dnd-beta-readiness-20260915`; the checked-out commit supplies `self`.
- Agent: `1a864a4674e1c538345647681e879180074d3389`.
- DND: `ccd9af68439b0d50948b64fdd8da472a1ae6742d`.
- Core: `9b6b9ed6205f3c5bcb0f10ba74209da71528d766` (0.3 lock metadata aligned).
- Source and normalized file hashes: `component-versions.dnd-beta.json`.
- Original multi-system deployment keeps its original component/dependency locks.
- DND source build layers `compose.dnd.yaml` after `compose.yaml`.
- Production order: `compose.yaml`, `compose.production.yaml`, `compose.dnd.yaml`,
  `compose.dnd.production.yaml`. Actual Compose resolution verified all application
  images use digests, without CoC or application source builds.

## Six review findings — closure

| Finding | Final behavior and evidence |
| --- | --- |
| Default Agent dependency mismatch | Original lock preserved; candidate uses its own hashed requirements and source SHAs. Agent image installation and `uv pip check` pass. |
| Recovery skipped persistence/accounting | Recovered result is persisted; unknown usage gets an explicit reconciliation audit. User token hold is released without inventing zero provider consumption. Independent monetary unknown holds remain fail-closed. Room recovery regression checks result and reservation. |
| Unknown action hid earlier results | Confirmed resolutions are published. Unknown action stays non-retryable; current DM reads its original receipt. Late receipts resume presentation without model/action replay. Reconciliation regression passes. |
| Natural-language combat entry unreachable | Owner/DM play/action now has `combat_start`; player authority remains unchanged. Tool-policy tests pass. |
| Generic combat rolls removed | One pre-mutation handoff selects a bounded roll catalog and continues the same request. Both model stages count toward usage. Handoff regression passes. |
| Optional party size still required | Domain validator, runtime schema and generated contract accept omission. Supplied values stay validated. Content-package tests and actual DND E2E omit the field. |

The historical change `4d362f7b` was already present. The earlier missing-branch
explanation was incorrect; residual schemas caused the contradiction.

## Original discussion acceptance matrix

| # | Requirement | Result and evidence |
| --- | --- | --- |
| 1 | Coherent release and ordinary install | Candidate lock; separate Agent requirements; Core/domain/runtime/MCP 0.3 wheels installed in a new Python 3.12 environment; dependency check and runtime lifecycle pass. Container imports resolve from `site-packages`. Diagnostic script reports versions, revisions, dirty state and hashes. |
| 2 | DND-only server | Enabled-system controls govern form, creation and readiness. Actual stack starts without CoC/Narrative. Flash preset has only DND MCP. |
| 3 | Two-user authorization/private media | Container E2E covers owner/player, private packs/artifacts, identity assignment and revocation. Agent histories are partitioned by trusted requester/actor/audience; Runtime call-time checks remain authoritative. |
| 4 | Natural language/bounded tasks | Owner/DM action can start combat. Stable phase/role/task catalog stays at most 16. Roll handoff does not ask the user to repeat the request. |
| 5 | Combat operations | Focused MCP tests cover attacks, 2024 core, movement/terrain, spell costs, parry/Uncanny Dodge/Shield, opportunity lifecycle, choices and end turn. This does not claim every official mechanic is complete. |
| 6 | Mechanics/narration/pending truth | Host requires actual authorized resolution IDs. Pending reactions stay pending. Public output cannot cite a DM-only resolution. |
| 7 | Commit succeeded/final output missing | Durable dispatch/return journal; recovery projects original receipts without model calls or action replay. Earlier confirmed results survive a later uncertain operation. |
| 8 | Cancel/errors/budget/unknown outcome | Cancellation preserves committed state. Known partial usage propagates and settles. Handoff sums usage. Unknown usage is audited. Unknown actions use read-only receipt reconciliation, never automatic re-execution. Existing four-scope monetary budget controls remain fail-closed. |
| 9 | Restore/branch invalidation/current grants | Core execution/audit tests verify stale decision rejection and that restore never revives revoked private access. Combat tests reject invalid branches before rolling. |
| 10 | Hidden history pagination | Keyset scan fills visible pages across hidden rows; regression crosses 305 hidden messages. Eight Chromium room/account tests pass. |
| 11 | Backup/restore/continue | Real PostgreSQL/MinIO/DND/Agent backup with writers stopped and SHA-256 checks. Isolated restore uses exact recorded image IDs. A new operation in an existing restored campaign succeeds. |
| 12 | Real Flash/target HTTPS | Preset, request mapping and production input checks prepared. External acceptance remains unrun: hostname/server/real configuration absent. |

## Verified results

- Web full suite: **253 passed, 11 skipped**. Eight browser skips were separately
  enabled: **8 passed**. Other skips: two Windows symlink-dependent tests and one
  Narrative-workspace test outside this profile.
- New candidate lock/build/restore tests: **3 passed**; production preflight:
  **4 passed**. JavaScript state/API/service-worker tests: **23 passed**.
- Agent worker/journal/provider focused suite: **111 passed**.
- DND runtime/content/combat/reaction/authorization focused suite: **77 passed**.
- Core execution/audit completion: **24 passed**.
- Fresh wheel-only runtime lifecycle: **1 passed**, all 46 installed packages
  compatible. Generated-contract checks and Ruff pass.
- Final container E2E with deterministic mock provider: `status=ok`, campaign
  `22978da6-c0f0-4b1c-89bf-e4b335ada5f6`, private pack, no CoC/Narrative campaign.
  It ends in lobby; combat is tested separately at Runtime/MCP level.

Final backup: `D:/repo/SagaSmith/beta-backup-final-20260916`.
Source project: `sagasmith-dnd-beta-check`, API `http://127.0.0.1:18088`.
Restored project: `sagasmith-dnd-beta-restore-final`, API `http://127.0.0.1:18090`.
Restore checks found 5 campaigns, 8 private packs, 8 artifacts, 4 identities and
4 assignments. Continued job `56a66c7c-2632-4bf9-939c-f6f839387736` succeeded;
private pack reimport succeeded. Projects remain intact. Earlier incomplete
backup directories are not release evidence.

## Reproduce and prepare deployment

1. Verify pinned source and wheel hashes:

   ```powershell
   rtk uv run python scripts/beta_diagnostics.py --source-root .. --wheels ../beta-wheels-final --output ../beta-diagnostics-final.json
   ```

2. Run `dnd-beta-recovery` for Linux build, dual-user fixture, backup and continued
   restore. It uses a deterministic provider and stores evidence, without publishing.

3. Inspect the three-image build plan:

   ```powershell
   rtk uv run python scripts/build_dnd_release.py --registry ghcr.io/sagasmithai --version candidate-20260916 --plan
   ```

   Without `--plan`, images load locally. Publishing requires explicit `--push`
   after registry login, producing `release/dnd-beta/images.env`, metadata, SBOM
   and provenance. The generic four-image release workflow is not this candidate.

4. When the target is selected, copy `.env.dnd-production.example` to
   `.env.production` and the Flash preset to `secrets/agent-config.json`. Fill the
   actual secrets, private S3 details, reviewed exact-model prices and budgets:

   ```powershell
   rtk uv run python scripts/production_preflight.py --env-file .env.production
   ```

5. Before inviting users, verify HTTPS login, external private-object denial and
   revocation, paid Flash multi-turn combat/fault recovery, and isolated restore on
   that target. The blank template intentionally cannot pass production preflight.

## Recovery boundary

A missing receipt never proves an action did not commit. Reconciliation reads
`room-operation:<original UUID>` without issuing a new action/key. Current audience
checks govern recovered projections. User token holds with unavailable usage are
released with a reconciliation audit, not a zero-cost ledger entry. Unknown
monetary claims remain fail-closed and require actual provider evidence to settle.
