# Beta readiness work ledger

Scope: decisions in the 2026-09-14 trial-cost and launch-readiness discussion.
Implementation branch: `feat/beta-readiness-20260914`, based on `36bfe821`.
Agent dependency: [PR 54](https://github.com/SagaSmithAI/SagaSmith-agent/pull/54),
commit `2780a2134201f5c2dd66eae43f7bcfcb87efd880`. The lock names that review
branch until coordinated acceptance; this is a candidate, not a published release.

| Discussion item | Implementation | Remaining acceptance |
| --- | --- | --- |
| Offline administrator | Audited create/reset CLI; signup cannot elevate | Create real administrator offline |
| Invitation beta / zero grants | Atomic limited-use expiring invites; production rejects open signup/nonzero automatic grants | Issue invitations and finite operator grants |
| Rate limits | IP/site, normalized account, verified-session user buckets | Real proxy address verification |
| Usage vs entitlement | Uncapped provider ledger; grant-period allocation and rollover fixes | Real invoice reconciliation |
| Four budget scopes | Site/user/campaign/task per-attempt admission, expiring reviewed prices, idempotent settlement; unknown outcomes held | Configure actual model prices, bounds and budgets |
| Paid request paths | OpenAICompatProvider only; retry admission; no hidden protocol fallback; old workers rejected | Real provider failure/retry acceptance |
| Private S3 | Explicit provisioning, region, conditional writes; shared per-user upload quota | Actual Spaces private/revoked-user negative tests |
| Release/CI | MinIO digest fixed; four GHCR digest images with SBOM/provenance; explicit capability gate | Published images pulled on a clean host |
| Beta resources | 6 workers/1 spawn, 2 room jobs/1 per room, 1 module worker; separate model presets; container/log/tmp/upload limits | Peak resource/load measurements and generated-object monitoring |
| Observability/CSP | Bounded metric labels, cached readiness, hidden production details, consistent proxy CSP | Inspect real edge headers and logs |
| Backup/restore | Writers stopped; database/domain/workspace/object checksums; encryption; isolated empty target bucket/fresh volumes | Encrypted off-host backup and continued-play restore |
| Account/content policy | Deactivation vs erasure, deletion tombstones, backup retention and rights runbook | Actual contact, retention and approved beta-content list |
| Cost plan | Scenario calculator and explicit assumptions | Select host/model and approve spending |
| Discussion posts | Busy state, retained draft, idempotent replay; changed replay body rejected | Synchronous text posts incur no model quota and show no fictitious background queue |
| Existing frontend PR | [PR 53](https://github.com/SagaSmithAI/SagaSmith-Web/pull/53) retains room stale-result guards, transient retry and offline-shell work | Review alongside this branch; preserve invite field and PR 53 busy flow |
| Room / Module UX | Queue cancel/retry and SSE reconnect additions in this branch | Real transport reconnect acceptance |
| HTTPS / three systems | Edge checker requires all eight authenticated scenario evidence records | Real D&D, CoC and narrative play, private Pack access and restore |
| Private visibility | Viewer ADMIN; organization Free; Web currently public | User visibility/plan decision; private GHCR access unverified because token lacks package-list scope |

## Validation boundaries

Agent: 764 provider/hosted tests passed; changed-file Ruff and diff checks passed.
Web: Python regression, two real Chromium scenarios, 17 JavaScript state tests,
5/5 remote component ancestry checks and deployment/migration validation recorded in the PR.
The older captured Agent authority fixture retains its original source SHA; it was not
relabeled as new runtime evidence.

Docker Engine is unavailable locally. No production host, domain, bucket credentials,
paid-model acceptance budget or release-publishing authorization was supplied.
No merge, visibility change, release, cloud purchase or successful live restore is implied.
Worktrees are registered for recovery; cleanup preserves unknown ignored data.
