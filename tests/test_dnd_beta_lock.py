import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_script(name):
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_candidate_lock_matches_build_inputs_and_preserves_default_profile():
    lock = json.loads((ROOT / "component-versions.dnd-beta.json").read_text("utf-8"))
    compose = (ROOT / "compose.dnd.yaml").read_text("utf-8")
    for component in lock["components"]:
        if component["revision"] != "self":
            assert component["remote"] + "#" + component["revision"] in compose
    assert load_script("beta_diagnostics").inspect()["failures"] == []
    compiler = load_script("lock_agent_supervisor")
    normal = compiler._compile_command(Path("agent/pyproject.toml"))
    candidate = compiler._compile_command(Path("agent/pyproject.toml"), profile="dnd")
    assert "2026-08-29T10:00:00Z" in normal
    assert lock["runtime_locks"]["dependency_cutoff"] in candidate
    assert normal[-1] != candidate[-1]


def test_restore_overlay_uses_recorded_image_ids_instead_of_mutable_tags():
    manifest = {"images": [{"ContainerName": f"fixture-{name}-1", "ID": "sha256:" + "a" * 64}
                           for name in ("api", "module-worker", "agent", "dnd-mcp")]}
    output = load_script("restore_image_overlay").render(manifest)
    assert output.count("pull_policy: never") == 4
    assert output.count("build: !reset null") == 4
    assert "latest" not in output and "coc-mcp" not in output


def test_dnd_release_builds_all_and_only_the_candidate_images():
    builder = load_script("build_dnd_release")
    plan = builder.commands("example.invalid/test", "candidate", Path("out"), False)
    assert [name for name, _ in plan] == ["WEB", "AGENT", "DND"]
    assert all("--load" in command and "--push" not in command for _, command in plan)
    published = builder.commands("example.invalid/test", "candidate", Path("out"), True)
    assert all("--push" in command and "--sbom=true" in command for _, command in published)
