# SagaSmith Forge

Forge is the sharing and collaboration surface inside the private hosted Service. It is not a file
sharing site for rulebooks. Its unit of publication is an immutable, moderated SagaSmith Release.

## Supported objects

| Object | Shared form | Installation/result |
|---|---|---|
| Module | finalized `.sagasmith-pack` plus public metadata | MCP import, then optional explicit activation |
| Rule | declarative Addon/Core Rules Pack | MCP import, then optional activation; executable uploads rejected |
| Character | reusable blueprint backed by a preset Pack | MCP import into the campaign actor library; never clones a live actor |
| Soul | versioned semantic style and procedures | user library or pinned by an Identity |
| Skill | reusable Agent procedure metadata | user library; cannot introduce a fixed MCP tool list |
| Asset | rights-cleared metadata/reference | user library |
| Identity | persistent DM/Keeper/NPC subject using one Soul release | invited into a campaign; not downloaded or Forked |

All types share ownership, collaborators, provenance, license, visibility, tags, immutable Releases,
favorites, discussions, spoiler markers, reports and moderation. Their runtime effects remain
type-specific.

## Publication state machine

```text
Artifact draft
  -> Release draft
  -> hosted Agent semantic/copyright-risk review
  -> moderation_pending
  -> published | rejected
  -> withdrawn (moderation/report)
```

Public or unlisted publication requires a rights attestation, a supported license, an approved
Agent review and human moderation. `private_source` artifacts and Releases marked as containing
private source cannot be submitted. A published Release has no update API.

## Identity assignment

The campaign owner invites an available Identity and chooses whether the campaign owner or Identity
owner pays model quota. The Identity owner accepts. Only then does Service ask D&D MCP to grant
`agent:<identity-id>` the DM role. The assignment pins the current Soul Release and creates
`campaign:<campaign-id>:identity:<identity-id>:assignment:<assignment-id>` as the private memory
namespace. Revocation calls
MCP first and only then marks the assignment revoked.

The current hosted runtime activates D&D DM Identities. Keeper catalog/profile support exists for
the shared product model, but CoC assignment is rejected until the CoC hosted authority adapter is
enabled. This prevents a UI-only or fake Keeper path.

## Copyright boundary

- Commercial PDFs, extracted text, chunks, embeddings and private campaign Packs never enter the
  public catalog.
- Public Pack payloads stay in private object storage and have no raw download endpoint; permitted
  users install through Service and the authoritative MCP facade.
- Fork is enabled only for licenses that permit derivatives. ARR works may be installed but not
  Forked.
- Public discussion uses plain text and explicit spoiler markers. DM/private campaign memory is not
  a discussion audience and cannot be published into a Soul.
- Reports preserve uploader, moderator, request-id and resolution audit evidence.
