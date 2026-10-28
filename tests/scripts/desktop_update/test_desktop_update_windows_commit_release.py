"""Post-commit R6 checkout release and live-delegate publication.

Split by contract to retain the canonical runner's unchanged per-file budget.
Fixtures stay at their original import paths; no test functions are imported.
"""
from __future__ import annotations

from contextlib import ExitStack
from pathlib import Path
import subprocess

import pytest

from tests.scripts.desktop_update.test_desktop_update_windows_commit_outcome import (
    _CheckoutHolder, _handoff,
)


@pytest.mark.platforms('windows')
def test_result_finished_at_is_stamped_after_the_r6_release_wait(tmp_path: Path) -> None:
    # The relaunched Desktop drops a non-manual result whose finished_at is 30 minutes old; the
    # R6 wait lasts up to 2 h. The result must carry the time the hand-off actually finished.
    with ExitStack() as cleanup:
        holders = []

        def hold_checkout(home: Path, install: Path) -> None:
            holders.append(cleanup.enter_context(_CheckoutHolder(install / '.hermes-update.lock', home)))

        code, out, _, result, home = _handoff(tmp_path, '-NoGateway', prepare=hold_checkout)
        holders[0].finish()
    assert code == 0, out
    log = (home / 'logs/desktop-update-handoff.log').read_text(encoding='utf-8-sig')
    assert 'keeping the update marker' in log, log
    assert result['ok'] is True and result['finished_at'] >= int(holders[0].released), result
    assert result['finished_at'] > holders[0].initial_finished_at, result
    assert 'removed update marker (owned)' in log, log
    assert not (home / '.hermes-update-in-progress').exists()
    assert not list(home.glob('.hermes-update-result.json.*.tmp'))


@pytest.mark.platforms('windows')
def test_marker_with_a_live_delegate_is_handed_to_it_at_finish(tmp_path: Path) -> None:
    # A7 release rule: the owner deletes its claim unless a delegate still runs; then that
    # delegate becomes the owner (canonical body, started_at kept, no delegate line).
    pid_file = tmp_path / 'delegate.pid'
    try:
        code, out, _, _result, home = _handoff(tmp_path, HANDOFF_DELEGATE=str(pid_file))
        assert code == 0, out
        marker = (home / '.hermes-update-in-progress').read_bytes().decode().split('\n')
        assert marker[0] == pid_file.read_text(encoding='utf-8-sig'), marker
        assert marker[2].startswith('ct:') and marker[3:] == [''], marker
    finally:
        if pid_file.exists():
            subprocess.run(['taskkill', '/F', '/PID', pid_file.read_text(encoding='utf-8-sig')], capture_output=True, check=False)
