# Recent beta behavior review — 2026-09-15

Historical review, updated 2026-09-17: the six findings below have been repaired.
The [MCP/Runtime/Skill follow-up](mcp-runtime-skill-repair-20260917.md) also corrects
the nonexistent defensive-reaction tool name and adds actual combat acceptance.
The following original findings are retained as review history, not current defects.
See [final acceptance and repair evidence](beta-readiness-20260916.md).

Scope: this preparation task's working changes in Web, Agent and D&D, plus the
earlier party-size change traced to `4d362f7b`. This is not a claim that every
historical commit or every game rule has been audited. Core has no local changes.

## Confirmed findings

1. **P1 — Default Agent image and dependency lock disagree.**
   `compose.yaml:127` still builds Agent `2780a213`, whose pyproject requires
   `dulwich>=0.22,<1`. The regenerated requirements at
   `infrastructure/agent-supervisor-requirements.txt:654` install `dulwich==1.2.15`.
   The generic Dockerfile installs the project with `--no-deps` and does not run
   `uv pip check`, so this incompatible environment can be built silently. The
   successful local D&D image used an explicit newer source override, not this
   default. Publish one coherent candidate lock or isolate the D&D dependency lock.

2. **P1 — Recovered turns bypass persistence and quota settlement.**
   `api/rooms.py:1888` synthesizes `saved_result` from operation receipts, then
   `:1903` takes the saved-result branch. `_persist_agent_result` is only called
   in the fresh completion branch (`:2021`). Publication later marks the job
   succeeded without closing its reservation or saving the recovered result.
   Read-only inspection of the local PostgreSQL fixture returned a succeeded
   job whose reservation status was still `reserved`. Recovery must persist its
   own durable phase and reconcile usage; unknown usage must not be invented as
   zero (`room_operations.py:65`).

3. **P1 — One uncertain operation hides earlier confirmed operations.**
   `room_operations.py:31` raises as soon as any dispatched operation lacks a
   returned receipt. Example: attack A committed and its receipt was saved;
   action B times out. The whole turn fails before A's resolution is projected.
   There is no reconciliation endpoint or queued recovery path in the added
   code; a late receipt only updates the row and does not resume the failed job.
   Preserve and show confirmed resolutions separately from the uncertain action,
   and reconcile the latter using its original identity without rerolling.

4. **P1 — The added natural-language combat entry is unreachable from a room turn.**
   `room_tool_policy.py:72` adds `combat_start` only to play/narration.
   `api/rooms.py:1337` creates an Agent job only for action messages, and `:1609`
   forwards that exact message type as the task. The play/action list still has
   no `combat_start`. An owner asking to begin combat cannot reach the added
   capability through this flow. Use an authorized task-selection transition;
   do not silently upgrade a player's authority or require an unrelated mode.

5. **P2 — Combat capability replacement removes previously supported rolls.**
   `room_tool_policy.py:64` replaces `dnd_check` and `dnd_dice_roll` with
   `combat_reaction_defense` and `rule_search` to stay under 16 tools. The domain
   still supports both removed tools during combat; the replacement is not
   functionally equivalent for generic checks and dice requests. Choose bounded
   task-specific subsets rather than dropping existing capabilities globally.

6. **P2 — Optional party guidance still has contradictory schemas.**
   Historical commit `4d362f7b` made party-size recommendations optional and is
   already an ancestor of the checked-out candidate. It was incorrect to call
   this a missing-branch-update problem. The content validator still required
   the field; this task has changed that check, but
   `Sagasmith-dnd/packages/runtime/src/sagasmith_dnd_runtime/application.py:4919`
   still lists `party_size` in the model-facing required fields. Adding null
   fields to the E2E fixture was a workaround and has been withdrawn. Align both
   layers and validate a genuinely omitted field end to end.

## Verification limits and retained work

- Six Chromium room-resilience tests passed; twenty Agent worker/journal tests
  passed. These do not cover all findings above.
- The latest party-omission regression is currently failing because its new
  source-reference fixture lacks `chunk_hash` and `note`. This test is not
  evidence of a completed party-size fix.
- Full D&D container acceptance has not passed. Earlier failures exposed empty
  creation scope, nested character-query scope and shared requester history;
  fixes exist locally, but final integrated verification is still outstanding.
- Backup/isolated restore, coherent release locking and remaining launch
  acceptance work are unfinished. Domain, server and DeepSeek configuration are
  intentionally blank per the user. No production deployment or paid model
  validation has occurred.

Next repair order: dependency coherence and recovery accounting; then partial
operation reconciliation, reachable task selection, and schema alignment.
