import asyncio
import importlib.util
import sys
from pathlib import Path


def _load_benchmark_module():
    path = Path(__file__).parents[1] / "scripts" / "benchmark_async_hotpaths.py"
    spec = importlib.util.spec_from_file_location("benchmark_async_hotpaths", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


run_benchmark = _load_benchmark_module().run_benchmark


def test_benchmark_covers_remaining_hot_paths_and_transaction_boundaries(tmp_path) -> None:
    result = asyncio.run(
        run_benchmark(
            database_url=f"sqlite:///{(tmp_path / 'hotpaths.db').as_posix()}",
            concurrency=2,
            iterations=1,
            upstream_delay_seconds=0,
            lag_interval_seconds=0.002,
            simulated_db_delay_seconds=0,
            private_root=tmp_path,
            scenario_names=(
                "agent_message",
                "projection_refresh",
                "panel_action",
                "activity_callback",
            ),
        )
    )

    scenarios = {item["name"]: item for item in result["scenarios"]}
    assert scenarios.keys() == {
        "agent_message",
        "projection_refresh",
        "panel_action",
        "activity_callback",
    }
    assert all(item["status_counts"] == {"200": 2} for item in scenarios.values())
    assert all("event_loop_db_fraction_of_request_wall_time" in item for item in scenarios.values())
    assert all(
        "p95_upper_bound_ms" in statement for item in scenarios.values() for statement in item["db"]
    )
    assert scenarios["agent_message"]["upstream_transaction_probes"] == {
        "agent.complete": {"in_transaction": 0, "released": 2, "samples": 2},
        "mcp.campaign": {"in_transaction": 0, "released": 2, "samples": 2},
    }
    assert scenarios["projection_refresh"]["upstream_transaction_probes"] == {
        "mcp.panel_state": {"in_transaction": 0, "released": 2, "samples": 2}
    }
    assert scenarios["panel_action"]["upstream_transaction_probes"] == {
        "mcp.panel_state": {"in_transaction": 0, "released": 2, "samples": 2},
        "mcp.phase.set": {"in_transaction": 0, "released": 2, "samples": 2},
    }
    assert scenarios["activity_callback"]["upstream_transaction_probes"] == {}
