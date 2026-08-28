# SagaSmith Web repository rename record

The GitHub repository was renamed from `SagaSmithAI/SagaSmith-service` to
`SagaSmithAI/SagaSmith-Web`. GitHub redirects the historical repository URL, but current
documentation, release metadata, support routes, and build inputs must use the canonical Web URL.

## Stable implementation identifiers

The repository rename deliberately did not change these compatibility-sensitive identifiers:

- Python distribution and CLI: `sagasmith-service`
- Python package: `sagasmith_service`
- default Compose project: `sagasmith-service`
- existing database schema, environment, metric, volume, image, and log identifiers

These names may change only through a separately reviewed migration with a compatibility window
and a rollback plan. Do not mechanically replace `sagasmith_service` or `sagasmith-service` in
code and deployment configuration merely to match the repository name.

## Completed rename checks

- The canonical repository and product name is SagaSmith Web.
- Active organization and website links point to `SagaSmithAI/SagaSmith-Web`.
- Current component locks contain only active vertical repositories.
- Archived standalone MCP, Skills, UI, and Module Generator repositories are not release inputs.
- Local and hosted release contracts keep the same authoritative MCP handlers and schemas.

## Ongoing compatibility checks

Before every release:

1. Reject new documentation or build inputs that use the historical repository URL.
2. Run the component audit and container configuration checks.
3. Verify issue templates, support links, repository metadata, deployment checkouts, monitoring,
   and backup manifests still use the canonical repository URL.
4. Keep historical implementation identifiers explicit so operators do not mistake them for stale
   repository links.
