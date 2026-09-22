"""Pin an isolated same-host restore to the exact images recorded by backup."""

import argparse
import json
import re
from pathlib import Path


def render(manifest: dict) -> str:
    lines = ["services:"]
    for service in ("api", "module-worker", "agent", "dnd-mcp", "coc-mcp"):
        images = [row for row in manifest["images"]
                  if row["ContainerName"].endswith(f"-{service}-1")]
        if not images and service == "coc-mcp":
            continue
        if len(images) != 1 or not re.fullmatch(r"sha256:[0-9a-f]{64}", images[0]["ID"]):
            raise ValueError(f"backup lacks an unambiguous image for {service}")
        lines.extend([f"  {service}:", "    build: !reset null",
                      f"    image: {images[0]['ID']}", "    pull_policy: never"])
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("backup", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    manifest = json.loads((args.backup / "manifest.json").read_text("utf-8-sig"))
    args.output.write_text(render(manifest), encoding="utf-8")


if __name__ == "__main__":
    main()
