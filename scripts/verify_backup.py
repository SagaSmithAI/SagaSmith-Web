from __future__ import annotations

import argparse
import json
from pathlib import Path

from sagasmith_service.backup import verify_backup


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("backup_directory", type=Path)
    arguments = parser.parse_args()
    manifest = verify_backup(arguments.backup_directory)
    print(
        json.dumps(
            {
                "status": "ok",
                "service_release": manifest.get("service_release"),
                "created_at": manifest.get("created_at"),
            }
        )
    )


if __name__ == "__main__":
    main()
