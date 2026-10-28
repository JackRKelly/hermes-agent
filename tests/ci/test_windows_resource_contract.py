"""Windows OS-test worker overrides retain upstream defaults and native lanes."""
from pathlib import Path

import hermes_yaml as yaml
import pytest

from tests.ci import _gha_expr as gha

_REPO = Path(__file__).resolve().parents[2]
_VARIABLE = "HERMES_CI_WINDOWS_TEST_WORKERS"
_WORKFLOW = yaml.safe_load(
    (_REPO / ".github/workflows/tests-os.yml").read_text(encoding="utf-8")
)
_JOB = _WORKFLOW["jobs"]["os-tests"]
_ROWS = _JOB["strategy"]["matrix"]["include"]


@pytest.mark.parametrize(
    "variables,windows_workers",
    [({}, "16"), ({_VARIABLE: ""}, "16"), ({_VARIABLE: "2"}, "2")],
    ids=["unset", "empty", "two-workers"],
)
@pytest.mark.parametrize("row_index", range(5))
def test_os_test_worker_env_uses_repository_override_only_on_windows(
    row_index, variables, windows_workers
):
    # Render the actual matrix and step env with the existing Actions evaluator;
    # runner.arch is context data, not a spoof of the test process's platform.
    matrix = {
        key: gha.render(value, {"vars": variables})
        for key, value in _ROWS[row_index].items()
    }
    windows = row_index >= 2
    arch = "ARM64" if row_index >= 3 else "X64"
    assert matrix["marker"] == ("windows" if windows else "macos")
    context = {
        "vars": variables,
        "matrix": matrix,
        "runner": {"os": "Windows" if windows else "macOS", "arch": arch},
    }
    step = next(
        step for step in _JOB["steps"]
        if step.get("name") == "Run ${{ matrix.marker }} tests"
    )
    env = {key: gha.render(value, context) for key, value in step["env"].items()}
    assert env == {
        # Empty on macOS preserves the runner's automatic CPU-count selection.
        "HERMES_TEST_WORKERS": windows_workers if windows else "",
        "HERMES_TEST_SLICE": matrix.get("slice", ""),
        "OPENROUTER_API_KEY": "",
        "OPENAI_API_KEY": "",
        "NOUS_API_KEY": "",
    }
    assert gha.render(_JOB["timeout-minutes"], context) == (60 if arch == "ARM64" else 30)
