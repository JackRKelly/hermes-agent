"""Native lock/protocol tests; these do not claim to execute Windows PowerShell."""
from pathlib import Path
import json
import subprocess
import sys
import threading
import time

import pytest

from tests.scripts.desktop_update import test_desktop_update_windows_commit_outcome as fixture


def _probe(path: Path) -> int:
    return subprocess.run([sys.executable, '-c', """
import os, sys
fd = os.open(sys.argv[1], os.O_RDWR)
os.lseek(fd, 1 << 20, os.SEEK_SET)
try:
    if os.name == 'nt':
        import msvcrt
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
    else:
        import fcntl
        fcntl.lockf(fd, fcntl.LOCK_EX | fcntl.LOCK_NB, 1)
except OSError:
    sys.exit(7)
""", str(path)], capture_output=True, timeout=10, check=False).returncode


def test_checkout_holder_waits_for_real_r6_before_releasing(tmp_path):
    home = tmp_path / 'home'
    (home / 'logs').mkdir(parents=True)
    lock = tmp_path / 'checkout.lock'
    initial = int(time.time())
    (home / '.hermes-update-result.json').write_text(json.dumps({'finished_at': initial}))
    marker = home / '.hermes-update-in-progress'
    marker.write_text('test owner')
    with fixture._CheckoutHolder(lock, home) as holder:
        assert _probe(lock) == 7  # READY follows actual native lock acquisition.
        assert holder.released is None
        # An unrelated log line must not authorize release.
        log = home / 'logs/desktop-update-handoff.log'
        log.write_text('update complete\n')
        assert _probe(lock) == 7
        log.write_text('keeping the update marker\n')
        holder.finish()
        assert holder.released >= initial + 1
        assert holder.initial_finished_at == initial
        assert _probe(lock) == 0
    assert holder.proc.poll() == 0
    assert holder.proc.stdin.closed and holder.proc.stdout.closed


def test_checkout_holder_eof_cleanup_unlocks_without_observed_wait(tmp_path):
    with fixture._CheckoutHolder(tmp_path / 'checkout.lock', tmp_path) as holder:
        assert _probe(tmp_path / 'checkout.lock') == 7
    assert holder.released is None
    assert holder.proc.poll() == 0
    assert _probe(tmp_path / 'checkout.lock') == 0
    assert not holder.observer.is_alive() and not holder.reader.is_alive()


def test_checkout_holder_rejects_failed_acquisition(tmp_path, monkeypatch):
    monkeypatch.setattr(fixture, '_CHECKOUT_HOLDER', "raise SystemExit('lock failed')")
    holder = fixture._CheckoutHolder(tmp_path / 'checkout.lock', tmp_path, timeout=2)
    with pytest.raises(AssertionError, match='checkout lock was not acquired'):
        with holder:
            pytest.fail('failed lock acquisition was reported ready')
    assert holder.proc.poll() is not None
    assert holder.proc.stdin.closed and holder.proc.stdout.closed
    assert not holder.reader.is_alive()


def test_checkout_holder_cancels_a_missing_release_ack(tmp_path, monkeypatch):
    # A broken helper says READY but never acknowledges RELEASE. The parent
    # must be able to cancel its queue read, close stdin, and reap the child.
    monkeypatch.setattr(fixture, '_CHECKOUT_HOLDER', """
import sys
print('locked', flush=True)
sys.stdin.readline()
sys.stdin.read()
""")
    (tmp_path / 'logs').mkdir()
    (tmp_path / 'logs/desktop-update-handoff.log').write_text('keeping the update marker')
    (tmp_path / '.hermes-update-result.json').write_text(json.dumps({'finished_at': 0}))
    (tmp_path / '.hermes-update-in-progress').touch()
    awaiting_ack = threading.Event()
    original = fixture._CheckoutHolder._message

    def message(self):
        if self.observer is not None:
            awaiting_ack.set()
        return original(self)

    monkeypatch.setattr(fixture._CheckoutHolder, '_message', message)
    with fixture._CheckoutHolder(tmp_path / 'checkout.lock', tmp_path) as holder:
        assert awaiting_ack.wait(timeout=2), 'observer never requested the release ack'
        assert holder.proc.poll() is None
        cleanup_started = time.monotonic()
    assert time.monotonic() - cleanup_started < 2, 'cancellation waited for the protocol deadline'
    assert holder.proc.poll() == 0
    assert not holder.observer.is_alive() and not holder.reader.is_alive()


def test_checkout_holder_reports_missing_r6_without_leaking(tmp_path):
    with fixture._CheckoutHolder(tmp_path / 'checkout.lock', tmp_path, timeout=2) as holder:
        with pytest.raises(AssertionError, match='R6 wait was not observed'):
            holder.finish()
    assert holder.proc.poll() == 0
    assert _probe(tmp_path / 'checkout.lock') == 0
