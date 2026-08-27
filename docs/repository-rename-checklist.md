# SagaSmith Web repository rename checklist

The product is **SagaSmith Web** now. This repository deliberately remains
`SagaSmithAI/SagaSmith-service` during the first naming phase so product language can settle
without combining it with a deployment and import migration.

## Stable names during phase one

- Repository: `SagaSmithAI/SagaSmith-service`
- Python distribution and CLI: `sagasmith-service`
- Python package: `sagasmith_service`
- Existing Compose project, service, image, volume, metric, and environment identifiers

Documentation should say **SagaSmith Web** for the product and use `SagaSmith-service` only when
referring to the repository or an implementation identifier.

## Phase two prerequisites

Do not rename the repository until all of these are true:

- hosted releases build only from the active revisions in `component-versions.json`;
- local stdio, local Streamable HTTP, and hosted network contract suites agree on schemas,
  capability discovery, errors, authority, revision, and idempotency;
- no release, installer, Compose file, or CI workflow reads an archived standalone repository;
- deployment owners have scheduled a maintenance window and rollback path; and
- links and automation that cannot follow GitHub redirects have been inventoried.

## Repository rename execution

1. Rename the GitHub repository from `SagaSmith-service` to `SagaSmith-web` in one scheduled
   operation; do not rename or archive another repository in the same change.
2. Update organization profile, website, active repository READMEs, issue/PR templates, support
   routes, release metadata, and `component-versions.json` to the new canonical URL.
3. Update local remotes, sibling-worktree scripts, immutable Git build contexts, Compose workspace
   overrides, deployment checkouts, backup/restore manifests, and component audit tooling.
4. Update branch protection, environments, webhooks, deploy keys, GitHub Apps, package metadata,
   Pages or container settings, and external status/monitoring links that are keyed by repository
   name.
5. Re-run link checks, the component audit, unit tests, container configuration, hosted acceptance,
   backup/restore, and one local/hosted MCP contract parity run.
6. Keep a time-bounded redirect observation period and document rollback to the old repository
   name if a release consumer cannot resolve the new location.

## Deliberately deferred implementation identifiers

The repository rename does **not** require changing `sagasmith_service`, `sagasmith-service`,
database schemas, environment keys, Compose project names, container names, metrics, or log labels.
Each may be migrated later only with a compatibility window, explicit operational value, and its
own tested change. Avoid a mechanical global replacement.
