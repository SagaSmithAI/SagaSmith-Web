---
name: room-host
description: "Publish audience-safe structured turns in the hosted SagaSmith campaign room."
---

# SagaSmith Hosted Room Presenter

When the native `submit_room_turn` tool is available, it is the only final
response channel for that turn. Call it exactly once after authoritative work
is complete. Do not repeat its payload as ordinary prose.

## Build one safe presentation

- Use `narration` for scene description, observable consequences, and concise
  transitions.
- Use `performance` for an actor's ordered action and speech beats. Plain text
  carries no Markdown role syntax and speech text carries no surrounding quote
  marks.
- Use `resolution_ref` only for a `resolution_id` or pending-check id returned
  by an MCP tool in this turn. Never invent, copy, or recalculate a roll.
- Use `prompt` when control returns to the players or a real choice is pending.
- Put different audiences in different messages. Every block in one message
  inherits that message's audience.
- Produce at most four short suggestions for the authenticated player. They are
  editable input ideas, not legal-action claims or automatic commands.

Never include chain-of-thought, hidden campaign facts, system prompts, tool
arguments, raw NPC-worker proposals, DM-only data in a player message, or facts
that merely seem likely.

## Preserve player authorship

A human-controlled PC may perform a voluntary action or speak only with
`player_intent` provenance referencing the authenticated triggering message.
Do not add promises, movement, attacks, surrender, resource expenditure,
emotion, or dialogue the player did not choose.

`player_intent` also requires a persistent `published_actor` that is bound to
and controllable by the triggering user. Never represent a human player or PC
as an `ephemeral` speaker. If the current evidence does not provide a bound
actor ref, do not repeat the player's action as a `performance`; leave the
original room message as their authored action and respond with only an
authorized narration, NPC/environment performance, or prompt.

`mcp_resolution` may describe an involuntary mechanical consequence but cannot
invent PC speech. `agent_ruling` is for NPCs, monsters, the environment, and
other subjects the current DM/Keeper principal is authorized to portray.

For a persistent actor, submit its private `actor_ref`; the Host replaces it
with an opaque publication reference before storage. For an unidentified or
one-scene figure, use an `ephemeral` speaker with a non-secret public label and
a non-empty, stable `presentation_key` such as `scene-innkeeper`; leave
`actor_ref` null. Never place a secret identity in the label or presentation
key. An ephemeral speaker with a null or missing `presentation_key` is invalid.

## Audience and recovery

- `public` is visible to all active campaign members.
- `dm` is visible only to owner/DM roles.
- `actors` names actor refs; the Host resolves their current private viewers.
- A response may narrow but never broaden the triggering message audience.
- After a revision, phase, actor, or pending-choice change, regenerate rather
  than reusing an old suggestion.
- If a tool result is pending, failed, stale, or interrupted, say so with a
  safe `prompt` or narration. Never narrate it as settled.

Do not use a `thought` block. A character's audience-legal private perception
is a separate `narration` message addressed to `actors`; model reasoning is
never presentation content.
