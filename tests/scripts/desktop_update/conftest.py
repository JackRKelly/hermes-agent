"""Shared process fixture for partitioned desktop-update tests."""
import subprocess
import sys

import pytest


@pytest.fixture
def sleeper():
    proc = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(600)'])
    yield proc
    proc.kill()
    proc.wait()
