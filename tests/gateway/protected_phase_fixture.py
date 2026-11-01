"""CONTROLLED local HTTP / bot-delivery fixtures, NOT real publication proof."""
import asyncio
from contextlib import closing
from dataclasses import replace
import json
import pytest

from tests.gateway.test_protected_native_admission import SignedFixture, DurableFixtureLedger, fixture_source
from tests.gateway.old_dispatcher_fixture import export_old_dispatcher, old_tick


@pytest.fixture(scope="session")
def docker_runtime(tmp_path_factory):
    from tests.gateway.docker_boundary_config import build_boundary_runtime
    return build_boundary_runtime(tmp_path_factory.mktemp("phase-docker-runtime"))


@pytest.fixture(scope="session")
def legacy_core(tmp_path_factory):
    return export_old_dispatcher(tmp_path_factory)


async def run_two_actual_card_owners_reject_repair_approve(tmp_path, monkeypatch, docker_runtime, legacy_core, fault):
    from gateway.session_card_owner import card_owner_runtime
    from gateway.session_protected_phase import (HeldPhasePolicy, install_held_card, native_phase_bootstrap_expected,
        native_phase_expected, native_phase_commit, native_phase_readback)
    from gateway.session_protected_admission import NativeAdmissionBridge, NativeAdmissionBridgeFactory
    from gateway.session_protected_native_sdk import NativeSDKPolicy
    from gateway.session_protected_timing import PilotTiming
    from gateway.session_protected_docker import DockerLaunch
    from gateway.session_kanban import run_task
    from hermes_cli import kanban_db as kb
    from hermes_cli.kanban_db_connect import connect
    from hermes_state_runtime import get_session_admission
    from agent.secret_scope import set_secret_scope, reset_secret_scope
    monkeypatch.setenv('HERMES_KANBAN_HOME', str(tmp_path))
    (tmp_path/'config.yaml').write_text('kanban:\n  dispatch_in_gateway: true\n  review_dispatch: true\n  dispatch_interval: 60\nmodel:\n  default: CONTROLLED_PROVIDER_DISABLED\n  provider: custom\n  base_url: http://127.0.0.1:1\n')
    from hermes_constants import get_hermes_home
    monkeypatch.setattr('gateway.run._load_gateway_config', lambda: {'model': {'default':'controlled-model',
        'provider':'openai-codex' if get_hermes_home().name == 'reviewer' else 'anthropic'},'platform_toolsets':{'cli':[]}})
    from gateway.session_protected_admission import _source_identity
    try:
        _source_identity()
        source_reader=_source_identity
    except ValueError:
        source_reader=fixture_source
        monkeypatch.setattr('gateway.session_protected_admission._source_identity', fixture_source)
    homes={r:tmp_path/'profiles'/r for r in ('builder','reviewer')}
    for h in homes.values():
        h.mkdir(parents=True)
    monkeypatch.setattr('hermes_cli.profiles.resolve_profile_env', lambda n:str(homes[n]))
    (tmp_path/'allowed.txt').write_text('CONTROLLED_ARTIFACT\n')
    kb.create_board('controlled')
    with closing(connect(board='controlled')) as conn:
        tid=kb.create_task(conn,title='CONTROLLED SAME CARD',assignee='builder',workspace_kind='dir',workspace_path=str(tmp_path),
            model_override='controlled-model',max_retries=0)
        kb.block_task(conn,tid,reason='controlled prepared')
    policies={r:HeldPhasePolicy(graph_digest='a'*64,campaign_key='controlled',card_id='card',board='controlled',task_id=tid,
        role=r,implementer_profile='builder',reviewer_profile='reviewer',input_head='b'*40) for r in homes}
    signer,ledger=SignedFixture(),DurableFixtureLedger(tmp_path/'ledger.db')
    started=ledger.check({})['started_at']
    replies=['CONTROLLED_BUILDER',json.dumps({'verdict':'REQUEST_CHANGES','head':'c'*40}),
        'CONTROLLED_REPAIR',json.dumps({'verdict':'APPROVE','head':'d'*40})]
    posts=[]
    async def serve(reader,writer):
        header=await reader.readuntil(b'\r\n\r\n')
        length=int(next(l.split(b':',1)[1] for l in header.split(b'\r\n') if l.lower().startswith(b'content-length:')))
        request=json.loads(await reader.readexactly(length))
        posts.append(request)
        step=len(posts)%3
        text=replies.pop(0) if step == 0 else None
        if step != 1:
            assert 'CONTROLLED_ARTIFACT' in json.dumps(request)
        if b'responses ' in header.splitlines()[0]:
            response=dict(id='controlled-response',object='response',created_at=0,status='completed',model=request['model'],
                output=[dict(type='message',id='controlled-msg',role='assistant',status='completed',
                    content=[dict(type='output_text',text=text,annotations=[])])],usage=dict(input_tokens=1,output_tokens=1,total_tokens=2))
            if step:
                response['output']=[dict(type='function_call',id=f'fc-controlled-{step}',call_id=f'controlled-read-{step}',
                    name='read_file',arguments='{}',status='completed')]
            body=('event: response.completed\ndata: '+json.dumps(dict(type='response.completed',response=response))+'\n\n').encode()
            content_type=b'text/event-stream'
        else:
            response=dict(id='msg_controlled',type='message',role='assistant',model=request['model'],
                content=[dict(type='text',text=text)] if step == 0 else [dict(type='tool_use',id=f'controlled-read-{step}',name='read_file',input={})],
                stop_reason='end_turn' if step == 0 else 'tool_use',stop_sequence=None,usage=dict(input_tokens=1,output_tokens=1))
            body=json.dumps(response).encode()
            content_type=b'application/json'
        writer.write(b'HTTP/1.1 200 OK\r\nContent-Type: '+content_type+b'\r\nContent-Length: '+str(len(body)).encode()+b'\r\nConnection: close\r\n\r\n'+body)
        await writer.drain()
        writer.close()
        await writer.wait_closed()
    server=await asyncio.start_server(serve,'127.0.0.1',0)
    url=f'http://127.0.0.1:{server.sockets[0].getsockname()[1]}'
    def denied_discovery(*args, **kwargs):
        pytest.fail('card-only owner attempted ordinary tool/provider discovery')
    monkeypatch.setattr('hermes_cli.banner.compute_toolset_availability', denied_discovery)
    monkeypatch.setattr('agent.auxiliary_client.resolve_provider_client', denied_discovery)
    current_head=['b'*40]
    built=[]
    def installer(role):
        def install(authority):
            p=policies[role]
            provider='anthropic' if role == 'builder' else 'openai-codex'
            expected=native_phase_bootstrap_expected(authority,policy=p,controller_started_at=started, publication_identity={'app_id':123,'login':'controlled-review[bot]'})
            install_held_card(authority,policy=p,bootstrap_envelope=signer.sign(expected),verify=signer.verify,ledger=ledger,controller_started_at=started, publication_identity={'app_id':123,'login':'controlled-review[bot]'})
            def build(ref,row):
                sdk=NativeSDKPolicy(provider=provider,model='controlled-model',session_id=ref.session_id,base_url=url,accounting='subscription_request_bound',request_seconds=10,pilot_timing=PilotTiming())
                launch=DockerLaunch(tmp_path.resolve(),docker_runtime,tmp_path/'leases',('allowed.txt',),role)
                b=NativeAdmissionBridge(authority=authority,ref=ref,row=row,launch=launch,sdk_policy=sdk,envelope=None,verify=signer.verify,
                    ledger=ledger,resolve=lambda:dict(provider=provider,base_url=url,api_key='CONTROLLED_DUMMY',
                        api_mode='anthropic_messages' if provider == 'anthropic' else 'codex_responses'),
                    artifact_names=('allowed.txt',),controller_started_at=started,source_identity=source_reader,phase_policy=replace(p,input_head=current_head[0]))
                b.envelope=signer.sign(b.expected_authorization())
                built.append(b)
                return b
            authority._protected_admission_bridge=NativeAdmissionBridgeFactory(authority=authority,build=build)
        return install
    def action(owner,name,admission=None,head=None,publication=None):
        expected=native_phase_expected(owner.authority,board='controlled',task_id=tid,admission_id=admission,
            action=name,published_head=head,publication=publication)
        if publication is not None:
            for bad in (publication | {'actor':{'type':'Bot','app_id':999,'login':'other[bot]'}}, publication | {'head':'e'*40},publication | {'delivery_signature':''}):
                with pytest.raises(Exception):
                    native_phase_expected(owner.authority,board='controlled',task_id=tid,admission_id=admission,action=name,published_head=head,publication=bad)
        env=signer.sign(expected)
        for field,bad in [('owner',{}),('phase_policy',{}),('card',{}),('phase_sequence',-1),('previous_sha256','wrong')]:
            forged=signer.sign(expected | {field:bad})
            with pytest.raises(Exception):
                native_phase_commit(owner.authority,envelope=forged)
        with pytest.raises(Exception):
            native_phase_commit(owner.authority,envelope={**env,'signature':'unsigned'})
        response=native_phase_commit(owner.authority,envelope=env)
        assert native_phase_commit(owner.authority,envelope=env)==response
        if not name.startswith('claim_'):
            old_tick(legacy_core,tmp_path,tid)
        return response
    def pub(head,state):
        return dict(head=head,state=state,actor=dict(type='Bot',login='controlled-review[bot]',app_id=123),
            **({'publication_id':42} if state == 'PUBLISHED' else {'review_id':42}),
            delivery_signature='CONTROLLED independently verified delivery')
    async def execute(owner,context):
        token=set_secret_scope({},profile_home=owner.authority.profile_id)
        try:
            admitted=await run_task(owner.connection,context)
            aid=admitted['receipt']['admission_id']
            live=owner.authority.sessions[admitted['session_id']]
            if live.task:
                await asyncio.wait_for(asyncio.shield(live.task),30)
            row=get_session_admission(owner.authority.db,admission_id=aid)
            assert row['outcome']=='completed',row
            readback=native_phase_readback(owner.authority,admission_id=aid)
            assert readback['namespace_retired'] is True
            bridge=built[-1]
            assert readback['sdk_policy'] == {
                'provider': bridge.policy.provider, 'model': bridge.policy.model,
                'base_url': bridge.policy.base_url, 'accounting': bridge.policy.accounting,
            }
            cid=bridge._runner.turn.proxy.worker.container.cid
            bridge._runner.turn.proxy.worker.container.cid='0'*64
            with pytest.raises(Exception):
                native_phase_readback(owner.authority,admission_id=aid)
            bridge._runner.turn.proxy.worker.container.cid=cid
            epoch=owner.authority.epoch
            owner.authority.epoch+=1
            with pytest.raises(Exception):
                native_phase_readback(owner.authority,admission_id=aid)
            owner.authority.epoch=epoch
            source=bridge._source_identity
            bridge._source_identity=lambda:{'CONTROLLED_CHANGED_SOURCE':True}
            with pytest.raises(Exception):
                native_phase_readback(owner.authority,admission_id=aid)
            bridge._source_identity=source
            with closing(connect(board='controlled')) as conn:
                saved=kb.get_task(conn,tid).claim_expires
                conn.execute('UPDATE tasks SET claim_expires=1 WHERE id=?',(tid,))
                with pytest.raises(Exception):
                    native_phase_readback(owner.authority,admission_id=aid)
                old_tick(legacy_core,tmp_path,tid)
                conn.execute('UPDATE tasks SET claim_expires=? WHERE id=?',(saved,tid))
            assert readback['phase'] in ('builder_held','reviewer_held','repair_held')
            with closing(connect(board='controlled')) as conn:
                task=kb.get_task(conn,tid)
                assert task.status=='blocked' and task.current_run_id==context['run_id'] and task.claim_lock==context['claim_lock']
                assert kb.claim_task(conn,tid) is None
                assert kb.claim_review_task(conn,tid) is None
            before=len(posts)
            old_tick(legacy_core,tmp_path,tid)
            assert len(posts)==before
            return aid
        finally:
            reset_secret_scope(token)
    try:
        async with card_owner_runtime(profile_home=homes['builder'],kanban_home=tmp_path,install=installer('builder')) as builder:
            async with card_owner_runtime(profile_home=homes['reviewer'],kanban_home=tmp_path,install=installer('reviewer')) as reviewer:
                from gateway.session_card_owner import owned_card_authority
                from gateway.session_protected_admission import native_protected_readback
                ledger.observe=lambda binding:native_protected_readback(owned_card_authority(binding['native_binding']['profile_id']),binding)
                if fault == 'cross-wal':
                    actual_append=kb._append_event
                    def fail_projection(conn, task_id, kind, payload, **kwargs):
                        if kind == 'protected_phase' and payload.get('phase') == 'builder_held':
                            raise OSError('CONTROLLED cross-WAL projection fault')
                        return actual_append(conn,task_id,kind,payload,**kwargs)
                    monkeypatch.setattr(kb,'_append_event',fail_projection)
                    context=action(builder,'claim_builder')
                    admitted=await run_task(builder.connection,context)
                    aid=admitted['receipt']['admission_id']
                    live=builder.authority.sessions[admitted['session_id']]
                    if live.task:
                        await asyncio.wait_for(asyncio.shield(live.task),30)
                    assert get_session_admission(builder.authority.db,admission_id=aid)['outcome']=='completed'
                    with pytest.raises(Exception):
                        native_phase_readback(builder.authority,admission_id=aid)
                    old_tick(legacy_core,tmp_path,tid)
                    again=await run_task(builder.connection,context)
                    assert again['receipt']['admission_id']==aid and again['session_id']==admitted['session_id'] and len(posts)==3 and ledger.check({})['requests']==3
                    return
                a=await execute(builder,action(builder,'claim_builder'))
                if fault == 'stop-held':
                    from gateway.session_contract import SessionRef
                    bridge=built[-1]
                    await builder.connection.interrupt(SessionRef(builder.authority.profile_id,bridge.ref.session_id),{'execution_generation':bridge.row['generation']})
                    with pytest.raises(Exception):
                        action(builder,'request_review',a,'c'*40,pub('c'*40,'PUBLISHED'))
                    old_tick(legacy_core,tmp_path,tid)
                    assert len(posts)==3
                    return
                action(builder,'request_review',a,'c'*40,pub('c'*40,'PUBLISHED'))
                current_head[0]='c'*40
                builder.authority.retiring=True
                with pytest.raises(Exception):
                    action(reviewer,'claim_reviewer')
                builder.authority.retiring=False
                r=await execute(reviewer,action(reviewer,'claim_reviewer'))
                with pytest.raises(Exception):
                    action(reviewer,'approve',r,'c'*40,pub('c'*40,'APPROVED'))
                with closing(connect(board='controlled')) as conn:
                    claimed=conn.execute("SELECT id,payload FROM task_events WHERE task_id=? AND kind='claimed' ORDER BY id DESC LIMIT 1",(tid,)).fetchone()
                    original_claim=claimed['payload']
                    conn.execute('UPDATE task_events SET payload=? WHERE id=?',(json.dumps({'source_status':'ready'}),claimed['id']))
                    with pytest.raises(Exception):
                        action(reviewer,'request_changes',r,'c'*40,pub('c'*40,'CHANGES_REQUESTED'))
                    assert kb.get_task(conn,tid).status=='blocked'
                    conn.execute('UPDATE task_events SET payload=? WHERE id=?',(original_claim,claimed['id']))
                action(reviewer,'request_changes',r,'c'*40,pub('c'*40,'CHANGES_REQUESTED'))
                a=await execute(builder,action(builder,'claim_repair'))
                action(builder,'request_review',a,'d'*40,pub('d'*40,'PUBLISHED'))
                current_head[0]='d'*40
                r=await execute(reviewer,action(reviewer,'claim_reviewer'))
                assert ledger.check({})['requests']==12
                action(reviewer,'approve',r,'d'*40,pub('d'*40,'APPROVED'))
                with closing(connect(board='controlled')) as conn:
                    assert kb.get_task(conn,tid).status=='done'
                    assert conn.execute("SELECT count(*) FROM task_events WHERE task_id=? AND kind='completed'",(tid,)).fetchone()[0]==1
                    assert json.loads(kb._latest_event(conn,tid,'changes_requested')['payload'])['implementer']=='builder'
                assert len(posts)==len(ledger.proofs)==12 and ledger.check({})['requests']==12
                assert [b.policy.provider for b in built]==['anthropic','openai-codex','anthropic','openai-codex']
                assert all(b._runner.turn.proxy.worker.container.absent() for b in built)
        from gateway.runtime_ownership import ProfileOwnership
        probe=ProfileOwnership()
        probe.reserve(homes.values())
        probe.close()
    finally:
        server.close()
        await server.wait_closed()
