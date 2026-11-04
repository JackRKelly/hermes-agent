"""Isolated native card owner; no messaging or provider credentials."""
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_installer_sees_real_epoch_before_recovery_and_authenticated_connection(tmp_path, monkeypatch):
    from gateway.session_card_owner import card_owner_runtime
    from gateway.session_controls import AuthorityConnection
    from gateway.runtime_ownership import ProfileOwnership, OwnershipConflict
    import gateway.session_authority as native
    events = []
    recover = native.recover_session_inputs
    def recovered(db, **kwargs):
        assert events == ['install']
        events.append('recovery')
        return recover(db, **kwargs)
    monkeypatch.setattr(native, 'recover_session_inputs', recovered)
    home = tmp_path / 'profile'
    home.mkdir()
    def install(authority):
        assert authority.epoch > 0
        assert authority.sessions == {}
        events.append('install')
    async with card_owner_runtime(profile_home=home, kanban_home=tmp_path, install=install) as owner:
        assert isinstance(owner.connection, AuthorityConnection)
        assert owner.connection.native_owner
        assert owner.connection.authority is owner.authority
        assert owner.connection.actor.profile_id == str(home.resolve())
        assert owner.identify()['served_profiles'] == [{'profile_id': str(home.resolve()), 'home': str(home.resolve())}]
        rival = ProfileOwnership()
        with pytest.raises(OwnershipConflict):
            rival.reserve([home])
    assert events == ['install', 'recovery']
    assert owner.authority.retired
    from gateway.session_card_owner import owned_card_authority
    assert owned_card_authority(home) is None
    rival.reserve([home])
    rival.close()


@pytest.mark.asyncio
async def test_install_failure_releases_only_its_home(tmp_path):
    from gateway.session_card_owner import card_owner_runtime
    from gateway.runtime_ownership import ProfileOwnership
    home = tmp_path / 'profile'
    home.mkdir()
    def fail(authority):
        raise ValueError('controlled_install_failure')
    with pytest.raises(ValueError, match='controlled_install_failure'):
        async with card_owner_runtime(profile_home=home, kanban_home=tmp_path, install=fail):
            pytest.fail('installer failure published an owner')
    rival = ProfileOwnership()
    rival.reserve([home])
    rival.close()


@pytest.mark.asyncio
async def test_failed_exit_retains_the_real_reservation_until_exact_retry(tmp_path, monkeypatch):
    from gateway.session_card_owner import card_owner_runtime
    from gateway.runtime_ownership import ProfileOwnership, OwnershipConflict
    from gateway import run_runtime
    original=run_runtime._retire_profile_authority
    async def not_stopped(*args, **kwargs):
        return False
    monkeypatch.setattr(run_runtime,'_retire_profile_authority',not_stopped)
    home=tmp_path/'profile'
    home.mkdir()
    with pytest.raises(run_runtime.ProfileOwnershipRetained):
        async with card_owner_runtime(profile_home=home,kanban_home=tmp_path,install=lambda a:None) as owner:
            pass
    rival=ProfileOwnership()
    with pytest.raises(OwnershipConflict):
        rival.reserve([home])
    monkeypatch.setattr(run_runtime,'_retire_profile_authority',original)
    await owner.retry_retirement()
    rival.reserve([home])
    rival.close()


@pytest.mark.asyncio
async def test_selected_profile_must_be_absent_from_live_multiplexer_served_set(tmp_path):
    from gateway.session_card_owner import card_owner_runtime
    from gateway.control_socket import GatewayControlServer
    home=tmp_path/'profiles'/'builder'
    home.mkdir(parents=True)
    payload=dict(protocol=1,instance_id='CONTROLLED_STABLE',served_profiles=[{'profile_id':str(home),'home':str(home)}])
    control=GatewayControlServer(tmp_path,verb_handlers={'identify':lambda *a,**k:payload})
    assert await control.start()
    try:
        with pytest.raises(RuntimeError,match='served'):
            async with card_owner_runtime(profile_home=home,kanban_home=tmp_path,install=lambda a:None):
                pytest.fail('profile still served by stable multiplexer')
        assert not (home/'state.db').exists()
    finally:
        await control.stop()


@pytest.mark.asyncio
async def test_cancelled_startup_joins_epoch_write_before_releasing_ownership(tmp_path, monkeypatch):
    import asyncio
    from gateway.session_card_owner import card_owner_runtime
    from gateway.runtime_ownership import ProfileOwnership, OwnershipConflict
    import gateway.session_authority as native
    entered,release=asyncio.Event(),asyncio.Event()
    installed=[]
    actual=native.initialize_session_authority
    async def delayed(*args,**kwargs):
        entered.set()
        await release.wait()
        return await actual(*args,**kwargs)
    monkeypatch.setattr(native,'initialize_session_authority',delayed)
    home=tmp_path/'profile'
    home.mkdir()
    async def start():
        async with card_owner_runtime(profile_home=home,kanban_home=tmp_path,install=lambda a:installed.append(a.epoch)):
            pytest.fail('cancelled startup was published')
    task=asyncio.create_task(start())
    await entered.wait()
    task.cancel()
    # One loop boundary delivers cancellation; no clock-based delay.
    await asyncio.sleep(0)
    probe=ProfileOwnership()
    with pytest.raises(OwnershipConflict):
        probe.reserve([home])
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert len(installed)==1
    probe.reserve([home])
    probe.close()
