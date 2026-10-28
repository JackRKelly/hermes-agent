"""Ordinary Linux Python workers can match fork capacity without changing upstream defaults."""
from collections import Counter
import importlib.util
import json
import os
import re
from pathlib import Path
import subprocess
import sys

import hermes_yaml as yaml
import pytest

from tests.ci import _gha_expr as gha

_REPO = Path(__file__).resolve().parents[2]
_VARIABLE = "HERMES_CI_LINUX_TEST_WORKERS"
_JOB = yaml.safe_load((_REPO / ".github/workflows/tests.yml").read_text())["jobs"]["test"]
_STEP = next(step for step in _JOB["steps"] if step.get("name") == "Run tests")


def _render(value, context):
    # The shared evaluator's whole-template fullmatch spans multiple adjacent
    # templates. Render embedded expressions individually, retaining its real
    # operand/truthiness semantics rather than editing the shared helper.
    if not isinstance(value, str):
        return value
    pattern = re.compile(r"\$\{\{.*?\}\}")
    matches = list(pattern.finditer(value))
    if len(matches) == 1 and matches[0].span() == (0, len(value)):
        return gha.render(value, context)
    return pattern.sub(lambda match: gha.to_string(gha.render(match.group(), context)), value)


@pytest.mark.parametrize("variables,expected", [
    ({}, "32"), ({_VARIABLE: ""}, "32"), ({_VARIABLE: "2"}, "2"),
    ({"HERMES_CI_WINDOWS_TEST_WORKERS": "2"}, "32"),
    ({"HERMES_CI_LINUX_RUNNER": "ubuntu-24.04"}, "32"),
])
@pytest.mark.parametrize("slice_index", [1, 2])
def test_linux_worker_override_renders_for_every_existing_slice(variables, expected, slice_index):
    context = {"vars": variables, "matrix": {"slice": slice_index}, "strategy": {"job-total": 2}}
    env = {key: _render(value, context) for key, value in _STEP["env"].items()}
    assert str(env["HERMES_TEST_WORKERS"]) == expected
    assert env["HERMES_TEST_SLICE"] == f"{slice_index}/2"
    assert str(env["HERMES_TEST_FILE_TIMEOUT"]) == "600"
    assert all(env[key] == "" for key in ("OPENROUTER_API_KEY", "OPENAI_API_KEY", "NOUS_API_KEY"))
    assert _JOB["timeout-minutes"] == 30
    assert gha.render(_JOB["strategy"]["matrix"]["slice"], context) == [1, 2]
    assert _JOB["strategy"]["fail-fast"] is False
    assert _JOB["strategy"].get("max-parallel") == 2


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number", "2.5"])
def test_invalid_worker_override_fails_in_actual_runner_without_executing_tests(value):
    rendered = gha.render(_STEP["env"]["HERMES_TEST_WORKERS"], {"vars": {_VARIABLE: value}})
    assert str(rendered) == value, "bad input must not silently fall back to a different worker count"
    env = {key: val for key, val in os.environ.items()
           if key in {"PATH", "HOME", "TMPDIR", "SYSTEMROOT", "USERPROFILE", "TEMP", "TMP"}}
    env["HERMES_TEST_WORKERS"] = str(rendered)
    result = subprocess.run(
        [sys.executable, str(_REPO / "scripts/run_tests_parallel.py"),
         "--files", str(Path(__file__).resolve())],
        cwd=_REPO, env=env, text=True, capture_output=True, timeout=30, check=False,
    )
    assert result.returncode != 0
    assert "max_workers must be greater than 0" in result.stderr or "invalid literal for int()" in result.stderr
    assert "=== Summary:" not in result.stdout


@pytest.mark.parametrize("total", [2, 4, 8])
def test_existing_slice_routing_covers_discovered_files_once_despite_independent_caches(capsys, total):
    spec = importlib.util.spec_from_file_location("linux_resource_partition", _REPO / "scripts/run_tests_parallel.py")
    assert spec is not None and spec.loader is not None
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    files = runner._discover_files([_REPO / "tests"])
    variables = {"HERMES_CI_LINUX_TEST_SLICES": json.dumps(list(range(1, total + 1)))}
    rows = gha.render(_JOB["strategy"]["matrix"]["slice"], {"vars": variables})
    assert len(rows) == total
    slices = []
    for index in rows:
        cache = {str(path.relative_to(_REPO)): index * 11.0 for path in files[::index]}
        slices.append(runner._slice_files(list(reversed(files)), index, len(rows), cache, _REPO))
    counts = Counter(path for shard in slices for path in shard)
    assert set(counts) == set(files)
    assert all(count == 1 for count in counts.values())
    assert all(slices)


def _execute_step(tmp_path, variables, index, total, runner_exit=0):
    # Execute the actual workflow shell/Python body. Only the final suite
    # executable is a fixture: no product tests run in this wiring test.
    scripts = tmp_path / "scripts"
    scripts.mkdir(exist_ok=True)
    fake_runner = scripts / "run_tests.sh"
    fake_runner.write_text(
        '#!/usr/bin/env bash\n'
        'printf "%s:%s" "$HERMES_TEST_SLICE" "$HERMES_TEST_WORKERS" > invoked\n'
        f'exit {runner_exit}\n'
    )
    fake_runner.chmod(0o755)
    context = {"vars": variables, "matrix": {"slice": index}, "strategy": {"job-total": total}}
    env = {"PATH": os.environ.get("PATH", ""), "HERMES_PYTHON": sys.executable}
    env.update({key: str(_render(value, context)) for key, value in _STEP["env"].items()})
    result = subprocess.run(["bash", "-e", "-o", "pipefail", "-c", _STEP["run"]],
                            cwd=tmp_path, env=env, text=True, capture_output=True, timeout=30, check=False)
    return result, tmp_path / "invoked"


@pytest.mark.parametrize("raw,total", [(None, 2), ("", 2), ("[1,2,3,4]", 4), ("[1,2,3,4,5,6,7,8]", 8)])
def test_configurable_matrix_routes_every_actual_slice_without_adding_parallel_vms(tmp_path, raw, total):
    variables = {_VARIABLE: "2"}
    if raw is not None:
        variables["HERMES_CI_LINUX_TEST_SLICES"] = raw
    rows = gha.render(_JOB["strategy"]["matrix"]["slice"], {"vars": variables})
    assert rows == list(range(1, total + 1))
    assert _JOB["strategy"]["max-parallel"] == 2
    for index in rows:
        case = tmp_path / str(index)
        case.mkdir()
        result, invoked = _execute_step(case, variables, index, total)
        assert result.returncode == 0, result.stderr
        assert invoked.read_text() == f"{index}/{total}:2"
        context = {"vars": variables, "matrix": {"slice": index}, "strategy": {"job-total": total}}
        assert gha.render(_JOB["name"], context) == f"Run tests ({index}/{total})"


@pytest.mark.parametrize("raw", ["[]", "[1]", "[1,1]", "[1,3]", "[0,1]", "[2,1]", '[1,"2"]', "[true,2]", "{}", "null", "not-json"])
def test_invalid_slice_configuration_fails_before_suite_execution(tmp_path, raw):
    result, invoked = _execute_step(tmp_path, {"HERMES_CI_LINUX_TEST_SLICES": raw}, 1, 2)
    assert result.returncode != 0
    assert not invoked.exists()


@pytest.mark.parametrize("value", ["0", "-1", "not-a-number", "2.5"])
def test_invalid_worker_configuration_is_rejected_before_suite_execution(tmp_path, value):
    result, invoked = _execute_step(tmp_path, {_VARIABLE: value}, 1, 2)
    assert result.returncode != 0
    assert not invoked.exists()


@pytest.mark.parametrize("index,total", [(1, 4), (0, 2), (3, 2)])
def test_mismatched_actual_matrix_total_or_index_is_rejected(tmp_path, index, total):
    result, invoked = _execute_step(tmp_path, {}, index, total)
    assert result.returncode != 0
    assert not invoked.exists()


def test_suite_failure_is_not_hidden_by_resource_validation(tmp_path):
    result, invoked = _execute_step(tmp_path, {_VARIABLE: "2"}, 1, 2, runner_exit=7)
    assert invoked.read_text() == "1/2:2"
    assert result.returncode == 7
