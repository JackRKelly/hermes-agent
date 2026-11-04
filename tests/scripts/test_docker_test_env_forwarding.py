"""Exercise the real canonical wrapper's supervisor-only Docker allowlist."""
import json
import os
from pathlib import Path
import subprocess
import sys

import pytest


@pytest.mark.platforms("posix")
def test_canonical_wrapper_forwards_only_named_docker_test_variables(tmp_path):
    expected = {
        "HERMES_DOCKER_BOUNDARY_TEST": "1",
        "HERMES_TEST_DOCKER_EXECUTABLE": str(tmp_path / "docker executable"),
        "HERMES_TEST_DOCKER_ENDPOINT": "unix:///run/docker.sock",
        "HERMES_TEST_DOCKER_BASE_IMAGE": "sha256:" + "a" * 64,
        "HERMES_DOCKER_EVIDENCE": str(tmp_path / "evidence directory"),
    }
    forbidden = {
        "HERMES_TEST_DOCKER_UNKNOWN": "unknown-test-sentinel",
        "HERMES_DOCKER_UNKNOWN": "unknown-hermes-sentinel",
        "DOCKER_HOST": "tcp://unselected.invalid:2375",
        "DOCKER_AUTH_CONFIG": "credential-sentinel",
        "OPENAI_API_KEY": "credential-sentinel",
        "HERMES_HOME": str(tmp_path / "private-home-sentinel"),
        "UNRELATED_ENV_SENTINEL": "unrelated-sentinel",
    }
    observed = tmp_path / "observed.json"
    case = tmp_path / "test_controlled_docker_env_child.py"
    case.write_text(
        "import json, os\nfrom pathlib import Path\n"
        "def test_child_environment():\n"
        f"    names = {list(expected) + list(forbidden)!r}\n"
        f"    Path({str(observed)!r}).write_text(json.dumps(\n"
        "        {key: os.environ[key] for key in names if key in os.environ}))\n",
        encoding="utf-8",
    )
    root = Path(__file__).resolve().parents[2]
    env = {**os.environ, **expected, **forbidden,
           "HERMES_PYTHON": sys.executable, "HERMES_TEST_FILE_RETRIES": "0"}
    # An enclosing activated runner must not make this child depend on its PM state.
    env.pop("__HERMES_ACTIVATED", None)
    result = subprocess.run(
        ["bash", str(root / "scripts/run_tests.sh"), str(case), "-j", "1", "--fail-on-skip"],
        cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert observed.is_file(), result.stdout + result.stderr
    assert json.loads(observed.read_text()) == expected
