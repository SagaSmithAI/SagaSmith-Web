# Beta account and content operations

Before inviting users, publish the operator's contact address, deletion request route,
backup retention period and the content list approved for this beta. An unset contact or
retention period is a launch blocker. These are operational requirements, not a claim
that any particular jurisdiction's legal requirements have been reviewed.

## Access and deletion

Deactivation stops access and revokes sessions. It does not delete a person's uploaded
files, campaigns, messages, billing records or backups. The account UI and operator must
not describe deactivation as erasure.

For an authenticated deletion request, record a private request ID, verify ownership,
disable the account and revoke its sessions, then inventory its private uploads, module
sources, generated media, Agent workspace and identity memory. Resolve shared campaign
ownership with participants before removing shared records. Domain-owned game data must
be changed through its MCP contract; never delete domain tables directly from Web.

Delete eligible live objects and projections using their owning service, and retain only
the minimum audit/billing evidence required by the published policy. Record the outcome
and exceptions without copying content into the audit log. Record a deletion tombstone
outside ordinary backups: after a restore, reapply tombstones before enabling access.
Tell the requester when retained backups expire. Do not promise immediate backup erasure
unless every retained copy has actually been removed and verified.

## Approved content

The first beta offers original material and material with documented permission for the
specific hosted use and audience. A public repository, uploader attestation, reference-only
source or successful Pack validation does not establish redistribution rights. Record the
license, source, attribution and reviewer for each offered Pack. Do not bulk-enable the
content-library catalog on the strength of its repository visibility.

Use the existing private Pack and review/finalize/activation boundaries. Keep complaints
private, preserve the relevant audit evidence, and withdraw access while an unresolved
rights complaint is reviewed. Do not expose source documents in public releases or logs.

## Beta limits and recovery

Keep Module Studio administrator-controlled, publish finite quotas, and disable arbitrary
plugin/script/MCP installation. Pack and Module source uploads share a per-user upload
quota; this is not a limit on all generated campaign state. Monitor generated objects,
Agent workspaces and disk separately. Temporary exchange files are removed after their
runtime operation; after a crash, inspect and remove abandoned files only while writers
are stopped so an in-flight source is not removed.

Set and test backup retention before launch. Backups must be encrypted off-host with
separate credentials, preserve object checksums, and be restored in isolation before
opening registrations. Publish measured recovery time and recovery point only after
the full restore and continued-play acceptance succeeds.
