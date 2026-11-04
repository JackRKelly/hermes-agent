"""Fail-closed real-owner/Docker faults, in an independently bounded file."""
import pytest
from tests.gateway import protected_phase_fixture as phase

docker_runtime = phase.docker_runtime
legacy_core = phase.legacy_core


@pytest.mark.asyncio
@pytest.mark.parametrize('fault',['cross-wal','stop-held'])
async def test_phase_fault_keeps_old_dispatcher_out(tmp_path, monkeypatch, docker_runtime, legacy_core, fault):
    await phase.run_two_actual_card_owners_reject_repair_approve(tmp_path,monkeypatch,docker_runtime,legacy_core,fault)
