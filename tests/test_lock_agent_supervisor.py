import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "lock_agent_supervisor", ROOT / "scripts" / "lock_agent_supervisor.py"
)
assert SPEC is not None and SPEC.loader is not None
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_compile_command_pins_resolver_and_dependency_cutoff() -> None:
    command = MODULE._compile_command(Path("/agent/pyproject.toml"))

    assert command[:5] == ["uvx", "--from", "uv==0.11.25", "uv", "pip"]
    cutoff_index = command.index("--exclude-newer")
    assert command[cutoff_index + 1] == "2026-08-17T12:00:00Z"
    assert "--generate-hashes" in command
    assert "--no-annotate" in command
