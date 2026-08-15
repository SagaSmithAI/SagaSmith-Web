# Component audit — 2026-08-16

## Result

All 18 sibling repositories were fetched. Every checkout matches its configured upstream default
branch with `ahead=0`, `behind=0`, and no pre-existing worktree changes. Nine repositories moved
after the 2026-08-14 lock; six of them form the hosted D&D build set and are upgraded together.

## Changed repositories

| Repository | Previous | Current | Integrated effect |
|---|---:|---:|---|
| `SagaSmith-agent` | `211782e99269` | `873c71c9c132` | NPC conversation v3 and authorization-fingerprint context binding |
| `sagasmith-core` | `028cf1939127` | `7b36d3da232c` | snapshot integrity fixes, compressed state/revision/rule/import documents, WAL, atomic actor lifecycle and scene-profile drafts |
| `sagasmith-dnd` | `346ec2934421` | `0dce1aad7ad7` | portable combat-grid templates and canonical legacy Pack actors |
| `SagaSmith-dnd-mcp` | `2219d18aee53` | `740aa64aaabf` | transactional Pack imports, atomic actors, NPC v3, authorization refresh and editable Pack combat grids |
| `SagaSmith-dnd-skills` | `1e44c39b36e5` | `a45a3b371474` | NPC v3 host contract and evidence-bound combat-grid review workflow |
| `SagaSmith-dnd-content-library` | `3adca46b4685` | `cd3224767822` | private current-Pack library with 46 validated D&D/CoC archives; no public deployment |
| `SagaSmith-coc-mcp` | `d98502ac8684` | `908fefa505ce` | atomic actor imports, private NPC v3 transport and hardened authorization barrier |
| `SagaSmith-coc-skills` | `441e7c21f86d` | `8f547f371c47` | private NPC transport guidance |
| `SagaSmith-narrative-mcp` | `f6d84f837309` | `81f7c9303e7c` | persisted actor revisions, schema-head assertion, and current Core event/snapshot compatibility |

The other nine repositories remain at the revisions recorded in
[`component-versions.json`](../component-versions.json).

## Hosted integration

- Compose and `.env.example` pin the new Agent/Core/D&D/D&D MCP/D&D Skills revisions as one
  reviewed compatibility set. Module Gen remains at `6a6261f52a88` because it has no new commit.
- The latest Agent and D&D MCP now agree on required `host_context_binding` authorization
  fingerprints and NPC conversation schema v3. No compatibility branch or v2 fallback is kept.
- Core storage migrations through the current schema head are consumed only inside the MCP-owned
  state volume. Service does not open or rewrite those tables.
- Transactional Pack/actor imports and editable combat-grid templates enter the hosted product
  through the current MCP facade and installed D&D Skills. Service does not duplicate their
  mechanics or write the Pack workspace directly.
- CoC and Narrative revisions are tracked in the global lock but are not loaded into the hosted
  D&D authority. Each remains a separate complete local authority until its own hosted slice is
  explicitly accepted.

## Required release evidence

```powershell
uv run python scripts/audit_components.py --fetch --strict
uv run ruff check .
uv run pytest
```

The real integration gate additionally exercises the latest streamable-HTTP D&D MCP, current
Agent dynamic-tool refresh, authorization rebinding, NPC v3 validation, transactional Pack import,
combat-grid template compilation, and Core migration path.
