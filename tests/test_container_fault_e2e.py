import importlib.util
from collections.abc import Iterator
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "container_fault_e2e.py"
SPEC = importlib.util.spec_from_file_location("container_fault_e2e_script", SCRIPT)
assert SPEC is not None and SPEC.loader is not None
container_fault_e2e = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(container_fault_e2e)


def _states(*items: dict[str, object]) -> Iterator[dict[str, object]]:
    yield from items


def test_wait_compose_service_handles_stop_then_healthy_transitions(monkeypatch) -> None:
    states = _states(
        {"State": "running", "Health": "healthy", "ExitCode": 0},
        {"State": "exited", "Health": "", "ExitCode": 0},
        {"State": "running", "Health": "starting", "ExitCode": 0},
        {"State": "running", "Health": "healthy", "ExitCode": 0},
    )
    monkeypatch.setattr(
        container_fault_e2e,
        "_compose_service_state",
        lambda *_args, **_kwargs: next(states),
    )
    monkeypatch.setattr(container_fault_e2e.time, "sleep", lambda _seconds: None)

    stopped = container_fault_e2e._wait_compose_service(
        "test-project",
        ["compose.yaml"],
        "redis",
        expected_state="exited",
        attempts=3,
        interval=0,
    )
    healthy = container_fault_e2e._wait_compose_service(
        "test-project",
        ["compose.yaml"],
        "redis",
        expected_state="running",
        expected_health="healthy",
        attempts=3,
        interval=0,
    )

    assert stopped["State"] == "exited"
    assert healthy["Health"] == "healthy"


def test_wait_compose_service_times_out_with_last_state(monkeypatch) -> None:
    monkeypatch.setattr(
        container_fault_e2e,
        "_compose_service_state",
        lambda *_args, **_kwargs: {
            "State": "running",
            "Health": "healthy",
            "ExitCode": 0,
        },
    )
    monkeypatch.setattr(container_fault_e2e.time, "sleep", lambda _seconds: None)

    with pytest.raises(TimeoutError, match="redis.*exited.*running"):
        container_fault_e2e._wait_compose_service(
            "test-project",
            ["compose.yaml"],
            "redis",
            expected_state="exited",
            attempts=2,
            interval=0,
        )


def test_wait_compose_service_rejects_nonzero_container_exit(monkeypatch) -> None:
    monkeypatch.setattr(
        container_fault_e2e,
        "_compose_service_state",
        lambda *_args, **_kwargs: {
            "State": "exited",
            "Health": "unhealthy",
            "ExitCode": 137,
        },
    )

    with pytest.raises(RuntimeError, match="redis.*exit code 137"):
        container_fault_e2e._wait_compose_service(
            "test-project",
            ["compose.yaml"],
            "redis",
            expected_state="exited",
            attempts=2,
            interval=0,
        )
