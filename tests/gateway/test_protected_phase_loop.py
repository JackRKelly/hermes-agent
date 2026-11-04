"""Actual two-provider same-card loop; CONTROLLED, not public delivery proof."""
import pytest
from tests.gateway import protected_phase_fixture as phase

docker_runtime = phase.docker_runtime
legacy_core = phase.legacy_core


@pytest.mark.asyncio
async def test_two_actual_card_owners_reject_repair_approve(tmp_path, monkeypatch, docker_runtime, legacy_core):
    await phase.run_two_actual_card_owners_reject_repair_approve(tmp_path,monkeypatch,docker_runtime,legacy_core,'none')
