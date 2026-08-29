"""Exercise Redis fail-closed behavior, MCP reconnect, and Worker reaping."""

from __future__ import annotations

import argparse
import json
import subprocess
import time
import uuid
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]


def _compose_command(project: str, files: list[str]) -> list[str]:
    command = ["docker", "compose", "-p", project]
    for compose_file in files:
        command.extend(["-f", compose_file])
    return command


def _docker(project: str, files: list[str], *arguments: str) -> None:
    command = _compose_command(project, files)
    subprocess.run([*command, *arguments], cwd=ROOT, check=True)


def _compose_service_state(project: str, files: list[str], service: str) -> dict[str, object]:
    result = subprocess.run(
        [*_compose_command(project, files), "ps", "--all", "--format", "json", service],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    output = result.stdout.strip()
    if not output:
        raise RuntimeError(f"Compose service {service} has no container state")
    try:
        payload = json.loads(output)
    except json.JSONDecodeError:
        payload = [json.loads(line) for line in output.splitlines() if line.strip()]
    records = payload if isinstance(payload, list) else [payload]
    matches = [
        record
        for record in records
        if isinstance(record, dict) and str(record.get("Service") or "") == service
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Compose service {service} returned {len(matches)} matching container states"
        )
    return matches[0]


def _wait_compose_service(
    project: str,
    files: list[str],
    service: str,
    *,
    expected_state: str,
    expected_health: str | None = None,
    attempts: int,
    interval: float,
) -> dict[str, object]:
    if attempts < 1:
        raise ValueError("attempts must be positive")
    expected_states = {expected_state.lower()}
    if expected_state.lower() in {"exited", "stopped"}:
        expected_states.update({"exited", "stopped"})
    expected_health = expected_health.lower() if expected_health else None
    last: dict[str, object] | None = None
    for attempt in range(attempts):
        last = _compose_service_state(project, files, service)
        state = str(last.get("State") or "unknown").lower()
        health = str(last.get("Health") or "").lower()
        raw_exit_code = last.get("ExitCode")
        exit_code = int(raw_exit_code) if raw_exit_code not in {None, ""} else None
        if state in {"dead", "exited", "stopped"} and exit_code not in {None, 0}:
            raise RuntimeError(f"Compose service {service} exited with exit code {exit_code}")
        if state in expected_states and (expected_health is None or health == expected_health):
            return last
        if state in {"dead", "exited", "stopped"} and not expected_states.intersection(
            {"dead", "exited", "stopped"}
        ):
            raise RuntimeError(
                f"Compose service {service} exited with exit code {exit_code} "
                f"while waiting for {expected_state}"
            )
        if attempt + 1 < attempts:
            time.sleep(interval)
    assert last is not None
    state = str(last.get("State") or "unknown").lower()
    health = str(last.get("Health") or "").lower() or "none"
    raise TimeoutError(
        f"Compose service {service} did not reach {expected_state}"
        f"{f'/{expected_health}' if expected_health else ''}; "
        f"last state {state}, health {health}, exit code {last.get('ExitCode')}"
    )


def _wait_status(client: httpx.Client, path: str, status: int, *, attempts: int = 120) -> None:
    for _ in range(attempts):
        try:
            if client.get(path, timeout=10).status_code == status:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise RuntimeError(f"{path} did not reach HTTP {status}")


def _require(response: httpx.Response, status: int = 200):
    if response.status_code != status:
        raise RuntimeError(
            f"{response.request.method} {response.request.url} returned "
            f"{response.status_code}: {response.text}"
        )
    return response.json()


def run(*, base_url: str, agent_url: str, project: str, files: list[str]) -> None:
    origin = base_url.rstrip("/")
    headers = {"Origin": origin}
    with httpx.Client(base_url=origin, headers=headers, timeout=180) as client:
        _wait_status(client, "/api/ready", 200)
        _require(
            client.post(
                "/api/auth/login",
                json={
                    "email": "owner-e2e@example.com",
                    "password": "correct horse battery staple",
                },
            )
        )
        campaigns = _require(client.get("/api/campaigns"))
        dnd = next(item for item in campaigns if item.get("system_id") == "dnd5e")
        narrative = next(item for item in campaigns if item.get("system_id") == "narrative")
        conversation = _require(
            client.post(
                f"/api/campaigns/{dnd['id']}/agent/conversations",
                json={"title": "MCP restart continuity"},
            ),
            201,
        )
        first = _require(
            client.post(
                f"/api/campaigns/{dnd['id']}/agent/conversations/{conversation['id']}/messages",
                headers={"Idempotency-Key": f"fault-before-{uuid.uuid4().hex}"},
                json={"content": "Query the current character list through the native MCP."},
            )
        )
        if "dynamic MCP call completed" not in str(first.get("assistant_content") or ""):
            raise RuntimeError("pre-restart Agent call did not use native MCP")
        narrative_conversation = _require(
            client.post(
                f"/api/campaigns/{narrative['id']}/agent/conversations",
                json={"title": "Narrative Worker restart continuity"},
            ),
            201,
        )
        narrative_url = (
            f"/api/campaigns/{narrative['id']}/agent/conversations/"
            f"{narrative_conversation['id']}/messages"
        )
        narrative_first = _require(
            client.post(
                narrative_url,
                headers={"Idempotency-Key": f"narrative-before-{uuid.uuid4().hex}"},
                json={"content": "Query the Narrative actors through the native MCP."},
            )
        )
        if "dynamic MCP call completed" not in str(narrative_first.get("assistant_content") or ""):
            raise RuntimeError("pre-restart Narrative call did not use native MCP")

        _docker(project, files, "restart", "dnd-mcp", "coc-mcp")
        _docker(project, files, "up", "-d", "--wait", "dnd-mcp", "coc-mcp")
        _wait_status(client, "/api/ready", 200)
        second = _require(
            client.post(
                f"/api/campaigns/{dnd['id']}/agent/conversations/{conversation['id']}/messages",
                headers={"Idempotency-Key": f"fault-after-{uuid.uuid4().hex}"},
                json={"content": "Query the character list again after the MCP restart."},
            )
        )
        if "dynamic MCP call completed" not in str(second.get("assistant_content") or ""):
            raise RuntimeError("Agent did not recover its native MCP session after restart")

        worker_headers = {"Authorization": "Bearer e2e-internal-agent-key"}
        with httpx.Client(
            base_url=agent_url.rstrip("/"), headers=worker_headers, timeout=10
        ) as agent:
            workspace_before_restart = _require(agent.get("/health/workers"))
        if workspace_before_restart["workspace_count"] < 2:
            raise RuntimeError(
                "Hosted workspace registry did not retain both active conversations: "
                f"{workspace_before_restart}"
            )
        if workspace_before_restart["workspace_unknown_entries"] != 0:
            raise RuntimeError(
                "Hosted workspace registry reported unknown managed entries before restart: "
                f"{workspace_before_restart}"
            )

        _docker(project, files, "restart", "agent")
        _docker(project, files, "up", "-d", "--wait", "agent")
        _wait_status(client, "/api/ready", 200)
        narrative_second = _require(
            client.post(
                narrative_url,
                headers={"Idempotency-Key": f"narrative-after-{uuid.uuid4().hex}"},
                json={"content": "Query the Narrative actors after the Worker restart."},
            )
        )
        if "dynamic MCP call completed" not in str(narrative_second.get("assistant_content") or ""):
            raise RuntimeError("Narrative MCP did not recover after the Worker restart")

        _docker(project, files, "stop", "redis")
        _wait_compose_service(
            project,
            files,
            "redis",
            expected_state="exited",
            attempts=40,
            interval=0.25,
        )
        _wait_status(client, "/api/ready", 503)
        protected = client.post(
            "/api/auth/login",
            json={
                "email": "owner-e2e@example.com",
                "password": "correct horse battery staple",
            },
        )
        if protected.status_code != 503:
            raise RuntimeError(
                f"protected request did not fail closed without Redis: {protected.status_code}"
            )
        _docker(project, files, "start", "redis")
        _wait_compose_service(
            project,
            files,
            "redis",
            expected_state="running",
            expected_health="healthy",
            attempts=240,
            interval=0.5,
        )
        _wait_status(client, "/api/ready", 200)

    with httpx.Client(base_url=agent_url.rstrip("/"), headers=worker_headers, timeout=10) as agent:
        for _ in range(30):
            status = _require(agent.get("/health/workers"))
            if status["active_workers"] == 0 and not status["orphan_pids"]:
                break
            time.sleep(0.5)
        else:
            raise RuntimeError(f"Hosted Worker processes were not reaped: {status}")
        if status["workspace_count"] < workspace_before_restart["workspace_count"]:
            raise RuntimeError(
                "Hosted workspace registry lost a non-terminal conversation across restart: "
                f"before={workspace_before_restart}, after={status}"
            )
        if status["workspace_unknown_entries"] != 0:
            raise RuntimeError(f"Hosted workspace registry found unknown managed entries: {status}")
    print(
        json.dumps(
            {
                "status": "ok",
                "redis": "failed-closed-and-recovered",
                "mcp": "reconnected",
                "narrative_worker": "restarted-and-recovered",
                "workers": "reaped-without-orphans",
                "workspaces": "registered-and-recovered-across-restart",
            }
        )
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", default="http://127.0.0.1:18088")
    parser.add_argument("--agent-url", default="http://127.0.0.1:18910")
    parser.add_argument("--project", default="sagasmith-service-e2e")
    parser.add_argument(
        "--compose-file",
        action="append",
        default=[],
        help="Compose file, repeatable; defaults to compose.yaml + compose.e2e.yaml",
    )
    arguments = parser.parse_args()
    files = arguments.compose_file or ["compose.yaml", "compose.e2e.yaml"]
    run(
        base_url=arguments.base_url,
        agent_url=arguments.agent_url,
        project=arguments.project,
        files=files,
    )


if __name__ == "__main__":
    main()
