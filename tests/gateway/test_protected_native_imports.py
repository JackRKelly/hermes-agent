"""Named ordinary-entry witnesses and fresh async factory import-closure proof."""
import subprocess
import sys

import pytest


@pytest.mark.parametrize('entry', ['run_agent', 'tools', 'cron', 'gateway', 'agent'])
def test_ordinary_entry_does_not_load_protected_sdk_or_bridge(entry):
    script = f'''
import importlib, sys
importlib.import_module({entry!r})
assert 'gateway.session_protected_native_sdk' not in sys.modules
assert 'gateway.session_protected_admission' not in sys.modules
'''
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('provider', ['anthropic', 'openai-codex'])
def test_fresh_real_sdk_factory_and_request_never_discover_plugins_or_tools(provider):
    script = f'''
import sys
class Fence:
 def find_spec(self, name, *args):
  if name in {{'hermes_cli.auth', 'hermes_cli.runtime_provider', 'providers', 'model_tools', 'run_agent', 'agent.auxiliary_client', 'tools.registry', 'hermes_cli.plugins'}}:
   raise RuntimeError('discovery/import reached: ' + name)
sys.meta_path.insert(0, Fence())
from tests.gateway.test_protected_native_sdk import test_actual_sdk_complete_or_cancel_closes_peer
# Actual local TCP + actual AsyncOpenAI/AsyncAnthropic, dummy credentials only.
test_actual_sdk_complete_or_cancel_closes_peer({provider!r}, None)
'''
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr


@pytest.mark.parametrize('kind', ['anthropic_api', 'anthropic_oauth', 'codex_oauth'])
def test_fresh_native_tools_and_thinking_never_reach_discovery(kind):
    script = f'''
import sys
attempted = []
class Fence:
 def find_spec(self, name, *args):
  if name in {{'hermes_cli.auth', 'hermes_cli.runtime_provider', 'providers', 'model_tools', 'run_agent', 'agent.auxiliary_client', 'tools.registry', 'hermes_cli.plugins', 'hermes_cli.models'}}:
   attempted.append(name)
   raise RuntimeError('discovery/import reached: ' + name)
sys.meta_path.insert(0, Fence())
import pytest
from tests.gateway.test_protected_native_headers import _exercise_native_tool_roundtrip
with pytest.MonkeyPatch.context() as patches:
 _exercise_native_tool_roundtrip(patches, {kind!r})
assert not attempted, attempted
'''
    result = subprocess.run([sys.executable, '-c', script], capture_output=True, text=True, timeout=20, check=False)
    assert result.returncode == 0, result.stderr
