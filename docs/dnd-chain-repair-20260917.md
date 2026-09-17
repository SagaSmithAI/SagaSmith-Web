# DND chain repair — 2026-09-17

This candidate repairs the seven failures reproduced in the second DND audit.
The scope is Web room → hosted Agent → signed MCP → Runtime → domain storage.

## Corrected behavior

1. Runtime checks nested actor/character and campaign selectors against the
   authenticated campaign, even when the requester owns both campaigns.
2. Hosted writes advance their signed campaign revision only from the preceding
   authoritative write receipt. Character revisions cannot advance that chain.
   Model arguments cannot override the hosted revision; local clients retain
   their explicit revision argument.
3. A transport timeout or malformed response after dispatch retains the original
   operation fence. A new write is blocked until the original key is reconciled.
4. Combat start/end retain an atomic public command receipt. Exact replay works
   after the phase changes and after process restart. Changed requests and new
   commands still pass current phase, membership, role, and revision checks.
5. A bounded `combat_support` catalog exposes Ready, Hide adjudication, HP,
   concentration, and official items. Confirmed operations can precede a catalog
   continuation; their receipts and usage carry forward without re-execution.
   Unknown results, foreign receipts, concurrent revisions, and more than six
   handoffs stop continuation. Each selected catalog remains within 16 tools.
6. Agent consumes MCP `readOnlyHint`; queries no longer create write fences.
7. The mixed history/undo/redo `state_revision` facade is advertised as writable.

The room-host Skill documents continuation and original-key recovery. DND's
generated operation contract is regenerated from the executable Runtime.

## Verification

Regression tests exercise signed cross-campaign denial, multi-write revision
progress, timeout fencing, read hints, phase replay, transactional rollback,
restart replay, paid-action continuation, unknown/foreign/concurrent denial,
and cumulative usage settlement. The catalog gate reads the real Runtime and
validates every role/phase/task selection.

The deterministic container acceptance requires Search to spend the actor's
action and a second hosted write to advance the combat turn. Recovery acceptance
backs up the same built images and continues the restored campaign.

The full Web suite exposed a timestamp-tie assumption in the realtime test on
Windows. It now verifies both committed authorization epochs explicitly, rather
than relying on wall-clock ordering. Production realtime behavior is unchanged.

## Deployment boundary

`component-versions.dnd-beta.json` and `compose.dnd.yaml` identify the matching
candidate components. Synthetic provider and recovery evidence does not establish
live DeepSeek or HTTPS acceptance. Domain, server, and live provider configuration
remain intentionally unset at the user's request.
