# MCP, Runtime and Skill repair

This supersedes the combat/tool-catalog conclusions in the September 16 beta
report. Production domain, server and real DeepSeek configuration remain unset
by user choice. Local and deterministic-provider evidence does not constitute
live launch acceptance.

## Repaired behavior

1. Combat action selection contains `combat_check` and `combat_choice`, with 16
   tools total. The nonexistent public name `combat_reaction_defense` is removed;
   defensive reactions use `combat_choice(action="resolve_defense")`. Combat roll
   selection keeps actor checks, numeric checks and raw dice available separately.
2. Hosted and full Skills route actor checks to `combat_check`, noncombat actor
   checks to `character_check`, numerical calculations to `dnd_check`, and raw
   dice to `dnd_dice_roll`. Numeric helpers do not spend a combat action.
3. Runtime publishes and enforces strict per-action payload models for combat
   choices, readied actions/spells, damage/healing and movement. Missing fields,
   unknown fields and wrong primitive types fail before settlement. Existing
   engine validators retain ownership of source facts, declarations and plans.
   High-frequency output schemas describe required success/pending envelopes and
   typed metadata; errors remain a separate recoverable result variant.
4. The 58,858-line Runtime application factory is replaced by a service composer.
   Campaigns, characters, combat, attacks, spells, inventory, continuity, content,
   authoring, presentation and transaction support live in separate modules.
   Per-instance operation aliases do not mutate shared class functions. Existing
   atomic commits, RNG receipts, exact replay and edition/source boundaries remain.
5. Modern Skill entrypoints, templates and deep workflows use the stable catalog
   and trusted Host selection. Mandatory legacy negotiation was removed from the
   modern workflow and retained in the explicit legacy adapter guide. Generated
   operation references come from the Runtime registry. Party-size advice remains
   optional; no upfront party-count field was introduced.

## Regression gates

- `check_dnd_room_catalog.py` checks all 27 D&D phase/role/task selections against
  the actual built Runtime, including the owner/DM transition from Play into Combat.
  CI exports the catalog from the built D&D container, not a hand-maintained fixture.
- `container_e2e.py --dnd-only` now prepares a disposable encounter through public
  Runtime operations, then sends an owner/DM room request through Web, Agent and MCP.
  It requires committed Search state and a main-action budget of zero. The model
  uses only advertised arguments; trusted campaign and idempotency fields are
  injected by Agent. Hosted identity does not elevate a player's permissions.
- The same acceptance still checks two users, exact requester/acting-host audit
  identity, private objects, module generation without required party guidance,
  revocation, backup, isolated restore and continued play.
- MCP tests distinguish numeric calculations from actor-aware Search, validate
  actual result schemas and exact retry responses, and reject malformed payloads
  before any campaign lookup. Runtime tests verify composition and instance isolation.
- Skill regression scans prevent legacy handshake instructions from returning to
  modern entrypoints or deep workflows. `publish --check` detects catalog drift.

## Scope retained

No rule-language rewrite, permission relaxation, provider key, production endpoint,
or automatic player-choice substitution is part of this repair. Domain-specific
objects retain the owning engine's existing structure and source validators.
Use the candidate component lock for exact source revisions and the recovery CI
artifact for container acceptance evidence.
