# Component audit — 2026-08-14

## Result

All 18 SagaSmith repositories were fetched and compared with their configured upstream default
branch. Every clone was clean, `ahead=0`, `behind=0`, and already at the current remote revision
before Service integration work began. The immutable audit record is
[`component-versions.json`](../component-versions.json).

The hosted product remains D&D-first by explicit product scope. “Integrated” therefore means that
every repository was classified, every hosted D&D runtime dependency is pinned and built into the
correct authority/Agent layer, and non-D&D authorities remain complete local products instead of
being silently mixed into the D&D control plane.

## Version and integration matrix

| Repository | Revision | Update audited | Hosted disposition |
|---|---:|---|---|
| `.github` | `37397ff36d58` | platform authoring/combat boundaries | organization contract |
| `SagaSmith-agent` | `211782e99269` | unified local D&D/CoC/Narrative stack; current dynamic MCP host | pinned hosted Agent build |
| `sagasmith-core` | `028cf1939127` | current compressed snapshot storage; no legacy migration | pinned D&D authority dependency |
| `sagasmith-dnd` | `346ec2934421` | neutral core contracts; deterministic D&D boundary | pinned D&D authority dependency |
| `SagaSmith-dnd-mcp` | `2219d18aee53` | session isolation, recovery, current snapshots and Pack gateway | pinned sole hosted D&D authority |
| `SagaSmith-dnd-skills` | `1e44c39b36e5` | current ending/source and module guidance | installed in MCP and Agent images |
| `SagaSmith-module-gen-skills` | `6a6261f52a88` | canonical system-aware Module Pack guidance | installed in MCP and Agent images |
| `sagasmith-dnd-ui` | `051c155900fa` | real gateway sessions and canonical workbench | local UI reference; hosted browser uses authenticated Service BFF |
| `SagaSmith-dnd-content-library` | `3adca46b4685` | current Pack catalog schema | local/open catalog; not copied into private hosted storage |
| `sagasmith-coc` | `bd95caae9ad1` | complete CoC v2 mechanics and current snapshots | audited local system; not loaded into D&D process |
| `SagaSmith-coc-mcp` | `d98502ac8684` | authenticated gateway, dynamic tools, current snapshots | audited independent local authority |
| `SagaSmith-coc-skills` | `441e7c21f86d` | canonical CoC module/runtime workflows | audited local skills |
| `sagasmith-coc-ui` | `d6c52b9fa08a` | real CoC gateway workbench | audited local UI |
| `SagaSmith-narrative-mcp` | `f6d84f837309` | hardened system-neutral authority and snapshots | audited independent local authority |
| `SagaSmith-narrative-skills` | `48d2621aebb6` | hardened narrative runtime workflows | audited local skills |
| `SagaSmith-service` | `214b15a9cb39` | migration normalization after M4 | integration worktree base |
| `sagasmith-ui` | `ff0fae483553` | Apache-2.0 relicensing | legacy local shell; no current protocol authority |
| `SagaSmithAI.github.io` | `91ca1bb789f4` | public Pack/combat boundary documentation | public documentation |

## Integration changes

- Docker build contexts now default to reviewed commit SHAs, not moving `main` or `master` refs.
- The D&D MCP image now receives `SagaSmith-module-gen-skills` and explicitly sets
  `SAGASMITH_MODULEGEN_SKILLS_DIR`; current MCP Skill resources therefore exist in production.
- The hosted Agent image now contains both current D&D Skills and Module Gen Skills at distinct
  roots. The Agent no longer relies on one ambiguous parent directory.
- Hosted and E2E Agent configuration now uses the current session-scoped contract:
  `injectPrincipal`, `sessionScoped`, `toolTimeout`, resources/prompts, and mutable native tools.
- A repeatable audit command checks revision, branch, origin, worktree, upstream, and divergence.

## Deliberate non-merges

- CoC MCP and Narrative MCP are separate authorities. Loading them into the D&D MCP process or
  routing D&D campaigns to them would violate the one-authority rule. Their hosted adapters should
  enter Service only as separately versioned product slices with system-specific acceptance tests.
- Public D&D/CoC workbench gateways bind a server-controlled principal and are correct for local
  use. Exposing either gateway directly behind the multi-user cloud domain would collapse account
  isolation. The hosted browser continues through Service authentication and isolated Agent/MCP
  sessions.
- `sagasmith-ui` is not treated as a compatibility fallback. Current system UIs and the private
  Service Web are the only active UI protocols.
- Content library data and commercial/private source material are not vendored into Service.

## Reproduce the audit

```powershell
uv run python scripts/audit_components.py --fetch --strict
uv run python scripts/audit_components.py --scope build --strict --json
docker compose config
uv run pytest
uv run ruff check .
```

The all-repository audit reports the Service worktree as dirty while integration changes are under
development; strict failure applies only to components marked `enforced` in the lock.
