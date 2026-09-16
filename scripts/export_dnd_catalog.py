"""Run inside the built D&D image; never read a checked-in catalog snapshot."""

import json
from pathlib import Path
from tempfile import TemporaryDirectory

from sagasmith_dnd_runtime.application import create_runtime
from sagasmith_dnd_runtime.config import McpConfig

with TemporaryDirectory(prefix="dnd-catalog-") as directory:
    root = Path(directory)
    runtime = create_runtime(McpConfig(
        home=root, database_url=None, chroma_url=None, chroma_path_override=None,
        dnd_skills_dir=root / "skills", modulegen_skills_dir=root / "modulegen",
        auto_seed_rules=False,
    ))
    try:
        print(json.dumps(runtime.contract()))
    finally:
        runtime.close()
