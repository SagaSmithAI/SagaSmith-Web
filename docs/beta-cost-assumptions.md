# Trial cost inputs

Budget one shared VM for Web, workers, domain MCPs, PostgreSQL, Redis and Caddy.
Start capacity testing with 20 invited users in four groups, not 20 simultaneous
model calls. No GPU or separate VM per repository is assumed.

The discussion's VM and backup scenario was USD 48/month plus 30% daily VM backup;
domain and encrypted off-host backup are additional estimates. Recheck the selected
SKU, region and checkout price before purchasing. Production now uses external S3:
do not reuse the older fixed-cost total that assumed on-host MinIO.

DigitalOcean Spaces Standard is USD 5/month including 250 GiB storage and 1,024 GiB
outbound transfer, verified 2026-09-14 against the
[official pricing page](https://docs.digitalocean.com/products/spaces/details/pricing/).
Use USD 72–75 as a planning range for fixed monthly expenses, excluding tax and
overages. This is not an invoice quote or a measured capacity guarantee.

Model rates must be supplied explicitly to `scripts/estimate_beta_cost.py` together
with a pricing version/date. The tool intentionally has no default model prices.
Example using the discussion's hypothetical rate inputs:

```bash
python scripts/estimate_beta_cost.py --pricing-version scenario-2026-09-14 \
  --input-usd-per-million 0.15 --cached-usd-per-million 0.003 \
  --output-usd-per-million 0.60
```

Default usage is four monthly sessions per group, three hours per session and 30
group-wide operations per hour: 1,440 operations for 20 players. The 45,000 input
and 3,000 output tokens are already cumulative across model calls in one operation;
do not multiply them by the number of internal calls again. Cache hit rate 50%
and 20% extra model usage are assumptions. Budget Module Studio separately.

Measure actual billed input/cache/output, successful task cost, worker RSS and
stored bytes during a complete three-hour D&D, CoC and Narrative test before
raising capacity. Pin one verified model first, then compare another model under
the same acceptance scenarios. A low token price alone does not establish quality.

Private GitHub Actions consumes the organization's included minutes and storage;
budget CI and recovery drills too. The organization was verified as Free during
this task. See [GitHub Actions billing](https://docs.github.com/en/billing/concepts/product-billing/github-actions).
Repository privacy does not automatically establish container-package privacy.
