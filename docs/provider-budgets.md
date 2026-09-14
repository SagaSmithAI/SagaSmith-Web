# Provider budgets and reconciliation

Production requires `SAGASMITH_PROVIDER_BUDGET_ENABLED=true` and reviewed
`SAGASMITH_PROVIDER_PRICES` JSON. Each model entry contains the provider class,
pricing version, timezone-aware `valid_until`, input/cached/output USD rates per
million tokens, the provider's documented maximum billable input tokens, an
output-token limit and a request-byte limit. Use the highest applicable time-band
rate during the review period. Expired or missing prices stop admission.

The first beta accepts only `OpenAICompatProvider` (the reviewed text-only
OpenAI-compatible path). Provider fallback chains, Anthropic, Bedrock, Codex and
other adapters are rejected before a hosted request: their internal retry and
price dimensions need their own per-attempt accounting before they can be enabled.

The request-byte limit is a payload guard, not an exact tokenizer. Configure
`max_input_tokens` from the provider's enforced model context limit; reserve the
full reviewed input ceiling and actual requested output ceiling before each call.
Do not label a measured average as the maximum. The initial beta pricing schema
supports input/cache-read/output charges; cache-creation or other billing dimensions
require a separately reviewed adapter and must not be silently treated as free.

The Agent's generic per-attempt hook authorizes before each request, including
retries and finalization. The supervisor refuses an old worker that does not
advertise `per-attempt-v1`. Each authorization claims the site, payer, campaign
and task budgets in one transaction. Runtime never lets a browser choose those
identities. Prices are snapshotted on each attempt, so later configuration changes
do not reprice existing calls.

Configure the first three budgets through the offline operator environment:

```bash
python -m sagasmith_service.budget_cli set-limit --scope site --id default --usd 30 --days 30 --reason beta-month-1
python -m sagasmith_service.budget_cli set-limit --scope user --id USER_UUID --usd 2 --days 30 --reason invited-tester
python -m sagasmith_service.budget_cli set-limit --scope campaign --id CAMPAIGN_ID --usd 5 --days 30 --reason beta-group
```

These numbers are operator-selected examples, not provider prices. The task budget
is created once from `SAGASMITH_TASK_BUDGET_USD` (default USD 1). Updating a currently
active cap preserves its period and recorded consumption. User token entitlements
are a separate ledger and must also be granted through existing administration.
Monthly grants, permanent grants and purchased credits are not interchangeable.

Every provider result records uncapped consumption. User entitlement deductions
may remain below provider spend when the site subsidizes an overrun. Missing usage
is unknown, never zero. Missing callback responses, process death or incomplete
provider usage retain the reservation; further attempts for the same task stop.
An overrun is recorded in full and blocks the task for review.

```bash
python -m sagasmith_service.budget_cli list-pending
python -m sagasmith_service.budget_cli reconcile --call-id CALL_UUID \
  --input-tokens 1000 --cached-tokens 400 --output-tokens 100 \
  --request-id PROVIDER_REQUEST_ID --evidence PRIVATE_INVOICE_REFERENCE
```

Reconcile only against provider evidence. A timeout alone does not prove a zero
charge. Keep evidence references private; never paste prompts or credentials into
the command or audit log. Do not expire unknown reservations automatically.

Before launch, test the chosen provider's reported cache counts, structured tools,
response IDs, error usage and context/output bounds. Unit tests establish internal
accounting behavior; a bounded live acceptance run must establish that the chosen
provider bills within these assumptions. Domain state and task replay remain under
their existing idempotency and MCP authority contracts.
