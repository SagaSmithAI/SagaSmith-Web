"""Build the three locked D&D images; publishing requires the explicit --push flag."""

import argparse
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def commands(registry: str, version: str, output: Path, push: bool) -> list[tuple[str, list[str]]]:
    lock = json.loads((ROOT / "component-versions.dnd-beta.json").read_text("utf-8"))
    sources = {item["repository"]: f"{item['remote']}#{item['revision']}"
               for item in lock["components"] if item["revision"] != "self"}
    variants = (
        ("WEB", "Dockerfile", {}),
        ("AGENT", "infrastructure/Dockerfile.agent-dnd", {
            "agent_source": sources["SagaSmith-agent"],
            "dnd_domain": sources["sagasmith-dnd"],
        }),
        ("DND", "infrastructure/Dockerfile.dnd-beta", {
            "core": sources["sagasmith-core"], "dnd_domain": sources["sagasmith-dnd"],
        }),
    )
    result = []
    for name, dockerfile, contexts in variants:
        image = f"{registry.rstrip('/')}/sagasmith-dnd-beta-{name.lower()}:{version}"
        command = ["docker", "buildx", "build", "--file", dockerfile, "--tag", image,
                   "--metadata-file", str(output / f"{name.lower()}.json")]
        for key, value in contexts.items():
            command.extend(["--build-context", f"{key}={value}"])
        command.extend(["--provenance=mode=max", "--sbom=true", "--push"] if push else ["--load"])
        result.append((name, command + ["."]))
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--registry", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--output", type=Path, default=ROOT / "release/dnd-beta")
    parser.add_argument("--push", action="store_true")
    parser.add_argument("--plan", action="store_true")
    args = parser.parse_args()
    plan = commands(args.registry, args.version, args.output.resolve(), args.push)
    if args.plan:
        print(json.dumps(dict(plan), indent=2))
        return
    subprocess.run(["python", "scripts/beta_diagnostics.py"], cwd=ROOT, check=True)
    args.output.mkdir(parents=True, exist_ok=True)
    lines = []
    for name, command in plan:
        subprocess.run(command, cwd=ROOT, check=True)
        metadata = json.loads((args.output / f"{name.lower()}.json").read_text("utf-8"))
        image = command[command.index("--tag") + 1].rsplit(":", 1)[0]
        lines.append(f"SAGASMITH_{name}_IMAGE={image}@{metadata['containerimage.digest']}")
    (args.output / "images.env").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
