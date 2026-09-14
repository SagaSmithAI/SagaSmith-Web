"""Exercise the real production edge without mutating application state."""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from urllib.parse import urljoin

REQUIRED_SCENARIOS = (
    "account_session_and_secure_cookie",
    "campaign_join_and_authority",
    "dnd_coc_narrative_turns",
    "quota_reserve_settle_release_and_retry",
    "private_s3_pack_upload_read_and_revoke",
    "mcp_and_agent_restart_reconnect",
    "room_turn_single_terminal_recovery",
    "isolated_backup_restore_and_private_pack",
)


def get_json(base_url: str, path: str, timeout: float) -> tuple[int, object]:
    request = urllib.request.Request(urljoin(base_url.rstrip("/") + "/", path.lstrip("/")))
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status = response.status
            body = response.read(256 * 1024)
    except urllib.error.HTTPError as exc:
        status = exc.code
        body = exc.read(256 * 1024)
    try:
        return status, json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError):
        return status, {"body_type": "non-json"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="real beta origin, for example https://beta.example")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument(
        "--evidence-file",
        type=argparse.FileType("r", encoding="utf-8"),
        help="operator-produced JSON evidence for every authenticated launch scenario",
    )
    args = parser.parse_args()

    failures: list[str] = []
    observations: dict[str, object] = {"base_url": args.base_url.rstrip("/")}
    if not args.base_url.lower().startswith("https://"):
        failures.append("--base-url must use https for a production acceptance run")
    for path in ("/api/health", "/api/ready"):
        try:
            status, payload = get_json(args.base_url, path, args.timeout)
        except (OSError, ValueError) as exc:
            failures.append(f"{path} request failed: {type(exc).__name__}")
            continue
        observations[path] = {"status": status, "json": isinstance(payload, dict)}
        if status != 200:
            failures.append(f"{path} returned HTTP {status}")
        if not isinstance(payload, dict):
            failures.append(f"{path} did not return a JSON object")
        if path == "/api/ready" and isinstance(payload, dict):
            if payload.get("status") != "ready":
                failures.append("/api/ready did not report status=ready")

    evidence: dict[str, object] = {}
    if args.evidence_file is None:
        failures.append(
            "authenticated launch evidence is required; health/readiness alone is insufficient"
        )
        evidence = {name: {"status": "pending"} for name in REQUIRED_SCENARIOS}
    else:
        try:
            submitted = json.load(args.evidence_file)
        except json.JSONDecodeError:
            failures.append("--evidence-file must contain a JSON object")
            submitted = {}
        submitted_scenarios = submitted.get("scenarios") if isinstance(submitted, dict) else None
        if not isinstance(submitted_scenarios, dict):
            failures.append("evidence JSON must contain a scenarios object")
            submitted_scenarios = {}
        for name in REQUIRED_SCENARIOS:
            item = submitted_scenarios.get(name)
            if (
                not isinstance(item, dict)
                or item.get("status") != "passed"
                or not item.get("evidence")
            ):
                failures.append(
                    f"launch scenario {name} lacks explicit passed evidence"
                )
                evidence[name] = {"status": "pending"}
            else:
                evidence[name] = {"status": "passed"}
    print(
        json.dumps(
            {
                "observations": observations,
                "scenarios": evidence,
                "launch_ready": not failures,
            },
            sort_keys=True,
        )
    )
    if failures:
        for failure in failures:
            print(f"ERROR: {failure}", file=sys.stderr)
        return 1
    print(
        "Production edge acceptance passed; this does not prove authenticated user flows, "
        "backup restore, or release provenance."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
