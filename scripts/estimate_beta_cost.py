"""Reproducible scenario estimate; prices are inputs, never a spending cap."""

import argparse
import json
from decimal import Decimal


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--players", type=int, default=20)
    parser.add_argument("--group-size", type=int, default=5)
    parser.add_argument("--sessions", type=int, default=4)
    parser.add_argument("--hours", type=int, default=3)
    parser.add_argument("--group-actions-per-hour", type=int, default=30)
    parser.add_argument("--input-tokens", type=int, default=45000)
    parser.add_argument("--output-tokens", type=int, default=3000)
    parser.add_argument("--cache-fraction", type=Decimal, default=Decimal("0.5"))
    parser.add_argument("--input-usd-per-million", type=Decimal, required=True)
    parser.add_argument("--cached-usd-per-million", type=Decimal, required=True)
    parser.add_argument("--output-usd-per-million", type=Decimal, required=True)
    parser.add_argument("--pricing-version", required=True)
    parser.add_argument("--fixed-monthly-usd", type=Decimal, default=Decimal("72"))
    parser.add_argument("--model-margin", type=Decimal, default=Decimal("0.2"))
    args = parser.parse_args()
    values = vars(args)
    for name, value in values.items():
        if name != "pricing_version" and value < 0:
            parser.error(f"{name} cannot be negative")
    if args.group_size == 0 or not 0 <= args.cache_fraction <= 1:
        parser.error("group-size must be positive and cache-fraction must be between 0 and 1")
    groups = (args.players + args.group_size - 1) // args.group_size
    actions = groups * args.sessions * args.hours * args.group_actions_per_hour
    per_action = (
        args.input_tokens * (
            (1 - args.cache_fraction) * args.input_usd_per_million
            + args.cache_fraction * args.cached_usd_per_million
        ) + args.output_tokens * args.output_usd_per_million
    ) / 1_000_000
    model = actions * per_action * (1 + args.model_margin)
    print(json.dumps({
        "pricing_version": args.pricing_version, "groups": groups, "actions": actions,
        "model_monthly_usd": str(model.quantize(Decimal("0.01"))),
        "fixed_monthly_usd": str(args.fixed_monthly_usd),
        "total_monthly_usd": str((model + args.fixed_monthly_usd).quantize(Decimal("0.01"))),
        "limitations": "Scenario only; excludes tax, overages, module generation and setup fees.",
    }, indent=2))


if __name__ == "__main__":
    main()
