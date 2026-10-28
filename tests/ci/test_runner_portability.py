"""Repository runner overrides cover the CI call closure without changing defaults."""
from pathlib import Path

import hermes_yaml as yaml
import pytest

from tests.ci import _gha_expr as gha

_REPO = Path(__file__).resolve().parents[2]
# Separate OS/architecture variables deliberately prevent an x64 override from
# silently moving a native ARM64 check to an emulated or foreign architecture.
_RUNNERS = {
    "blacksmith-4vcpu-ubuntu-2404": ("HERMES_CI_LINUX_RUNNER", "ubuntu-24.04"),
    "blacksmith-32vcpu-ubuntu-2404": ("HERMES_CI_LINUX_RUNNER", "ubuntu-24.04"),
    "blacksmith-2vcpu-windows-2025": ("HERMES_CI_WINDOWS_RUNNER", "windows-2025"),
    "blacksmith-32vcpu-windows-2025": ("HERMES_CI_WINDOWS_RUNNER", "windows-2025"),
    "blacksmith-12vcpu-macos-latest": ("HERMES_CI_MACOS_RUNNER", "macos-latest"),
    "windows-latest-32-arm-core": ("HERMES_CI_WINDOWS_ARM64_RUNNER", "windows-11-arm"),
}


def _runner_cases():
    pending = [".github/workflows/ci.yaml", ".github/workflows/nix.yml"]
    seen = set()
    while pending:
        path = pending.pop()
        if path in seen:
            continue
        seen.add(path)
        workflow = yaml.safe_load((_REPO / path).read_text(encoding="utf-8"))
        for name, job in workflow["jobs"].items():
            called = job.get("uses", "")
            if called.startswith("./.github/workflows/"):
                pending.append(called[2:])
                continue
            if "runs-on" not in job:
                continue
            rows = job.get("strategy", {}).get("matrix", {}).get("include", [{}])
            for index, row in enumerate(rows):
                yield pytest.param(job["runs-on"], row, id=f"{Path(path).name}/{name}/{index}")


def _selected_runner(expression, row, variables):
    matrix = {key: gha.render(value, {"vars": variables}) for key, value in row.items()}
    return gha.render(expression, {"vars": variables, "matrix": matrix})


@pytest.mark.parametrize("expression,row", list(_runner_cases()))
def test_repository_runner_override_preserves_native_lane(expression, row):
    original = _selected_runner(expression, row, {})
    assert original in _RUNNERS, f"Unreviewed native runner: {original}"
    variable, hosted = _RUNNERS[original]
    # Absent and explicitly empty variables must both retain the upstream label.
    assert _selected_runner(expression, row, {variable: ""}) == original
    assert _selected_runner(expression, row, {variable: hosted}) == hosted
    # Another platform's override must not change this lane.
    unrelated = {key: value for key, value in _RUNNERS.values() if key != variable}
    assert _selected_runner(expression, row, unrelated) == original
