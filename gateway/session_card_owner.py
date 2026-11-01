"""One native card authority, without a gateway's messaging/cron bootstrap.

This is a trusted Python consumer, not a CLI/config-selected alternate owner.
ProfileOwnership is the cutover fence: an existing multiplexer's reservation
must have been released before entry. No force, replacement or global mutation.
"""
from contextlib import asynccontextmanager, contextmanager
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from gateway.session_authority import SessionAuthority
import uuid

from gateway.session_controls import AuthorityConnection

# Actual owned handles only, never DB paths or worker-selected remote owners.
_owned_card_authorities = {}
_retained_card_resources = {}


def owned_card_authority(home):
    authority = _owned_card_authorities.get(str(Path(home).resolve()))
    return None if authority is None or authority.retired or authority.retiring else authority


@contextmanager
def card_owner_scope(authority):
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    from agent.secret_scope import current_secret_scope_home, set_secret_scope, reset_secret_scope
    home = set_hermes_home_override(authority.profile_id)
    secrets = None
    if current_secret_scope_home() != authority.profile_id:
        secrets = set_secret_scope({}, profile_home=authority.profile_id)
    try:
        yield
    finally:
        if secrets is not None:
            reset_secret_scope(secrets)
        reset_hermes_home_override(home)


class _CardRunner:
    """Only the native authority's card/drain dependencies; no gateway startup."""
    _draining = False
    card_only = True
    def __init__(self):
        self.adapters = {}

    def _adapter_for_source(self, source):
        return self.adapters.get(source.platform)

    def _cached_agent_for(self, route):
        return None

    _resident_agent_for = _cached_agent_for


class _OwnerTransport:
    async def send(self, *args, **kwargs):
        pass  # no remote viewer; SessionEvents remains the canonical event stream


@dataclass(frozen=True)
class CardOwner:
    authority: "SessionAuthority"
    connection: AuthorityConnection

    async def retry_retirement(self):
        resources = _retained_card_resources.get(self.authority.profile_id)
        if resources is None or resources[0] is not self.authority:
            raise RuntimeError('no retained resources for this exact card owner')
        await _close_owned(resources)

    def identify(self):
        runner = self.authority.runner
        return dict(protocol=1, runtime_protocol=1, kind='card-owner',
            instance_id=self.authority.instance_id, hermes_home=self.authority.profile_id,
            state='retired' if self.authority.retired else 'ready',
            served_profiles=runner.session_authorities.served_profiles())


def _resolved_homes(profile_home, kanban_home):
    return Path(profile_home).resolve(strict=True), Path(kanban_home).resolve(strict=True)


@asynccontextmanager
async def card_owner_runtime(*, profile_home: Path, kanban_home: Path,
                             install: Callable[['SessionAuthority'], None]) -> AsyncIterator[CardOwner]:
    from gateway.config import GatewayConfig
    from gateway.control_socket import GatewayControlServer
    from gateway.runtime_bootstrap import TicketStore
    from gateway.runtime_ownership import ProfileOwnership
    from gateway.session import SessionStore
    from gateway.session_authorities import SessionAuthorities
    from gateway.session_authority import initialize_session_authority
    from hermes_constants import set_hermes_home_override, reset_hermes_home_override
    if not callable(install):
        raise TypeError('card owner requires a trusted synchronous installer')
    home, board_home = await asyncio.to_thread(_resolved_homes, profile_home, kanban_home)
    ownership = ProfileOwnership()
    await _require_stood_down(home)
    ownership.reserve([home])
    try:
        await _require_stood_down(home)
    except BaseException:
        ownership.close()
        raise
    token = set_hermes_home_override(home)
    runner, authority, control, db = _CardRunner(), None, None, None
    try:
        runner.config = GatewayConfig()
        runner.kanban_home = board_home
        from hermes_state import SessionDB
        db = SessionDB(home / 'state.db')
        runner.session_store = SessionStore(home / 'sessions', runner.config, db=db)
        runner._session_db = db
        if db is None or (await asyncio.to_thread(Path(db.db_path).resolve)).parent != home:
            raise RuntimeError('card owner store does not belong to reserved home')
        runner.session_authorities = SessionAuthorities(home)
        runner.session_runtime_descriptor = dict(instance_id=uuid.uuid4().hex, state='starting', served_profiles=[])
        def install_owned(built):
            _owned_card_authorities[str(home)] = built
            return install(built)
        from gateway.session_protected_scope import _drain_owned
        cancellations = []
        startup = asyncio.create_task(initialize_session_authority(runner, profile_id=str(home),
            instance_id=runner.session_runtime_descriptor['instance_id'], db=db, install=install_owned, card_only=True))
        authority = await _drain_owned(startup, cancellations)
        if cancellations:
            raise asyncio.CancelledError
        runner.session_authorities.add(home, authority)
        runner.session_runtime_descriptor.update(state='ready', served_profiles=runner.session_authorities.served_profiles())
        runner.session_ticket_store = TicketStore(authority.instance_id, frozenset({str(home)}))
        ticket = runner.session_ticket_store.mint(profile_id=str(home), subject='native-card-owner', purpose='interactive')
        grant = runner.session_ticket_store.redeem(ticket, profile_id=str(home), purpose='interactive')
        connection = AuthorityConnection(authority, _OwnerTransport(), dict(user_id=grant['subject'], provider='local',
            profile_id=grant['profile_id'], instance_id=grant['instance_id'],
            capabilities=grant['capabilities'], native_bootstrap=True), operator=True)
        owner = CardOwner(authority, connection)
        control = GatewayControlServer(home, verb_handlers={'identify': lambda *a, **k: owner.identify(),
                                                          'status': lambda *a, **k: owner.identify()})
        if not await control.start():
            raise RuntimeError('card owner control socket unavailable')
        yield owner
    finally:
        try:
            await _close_owned((authority, control, runner, db, ownership, home))
        finally:
            reset_hermes_home_override(token)


async def _require_stood_down(home):
    from gateway.control_socket import query_gateway_control, resolve_client_socket_path
    from gateway.runtime_ownership import live_runtime_owner
    selected = await asyncio.to_thread(query_gateway_control, home, 'identify')
    if selected is not None:
        raise RuntimeError('selected profile is still served by a native owner')
    if home.parent.name != 'profiles':
        return
    root = home.parent.parent
    if not resolve_client_socket_path(root) and live_runtime_owner(root) is None:
        return
    first = await asyncio.to_thread(query_gateway_control, root, 'identify')
    second = await asyncio.to_thread(query_gateway_control, root, 'identify')
    if (first is None or second is None or not first.get('instance_id')
            or first.get('instance_id') != second.get('instance_id')):
        raise RuntimeError('stable served-set removal cannot be verified')
    for identity in (first, second):
        served = identity.get('served_profiles')
        if not isinstance(served, list) or any(not isinstance(row, dict) for row in served):
            raise RuntimeError('stable served-set removal cannot be verified')
        if any(str(home) in (row.get('home'),row.get('profile_id')) for row in served):
            raise RuntimeError('selected profile is still served by stable multiplexer')


async def _close_owned(resources):
    from gateway.session_protected_scope import _drain_owned
    cancellations = []
    task = asyncio.create_task(_finish_close(resources))
    try:
        await _drain_owned(task, cancellations)
    except BaseException:
        # A local variable is NOT retention: unwinding would close the flock
        # descriptor. Keep exact handles reachable for an explicit same-owner retry.
        _retained_card_resources[str(resources[-1])] = resources
        raise
    if cancellations:
        raise asyncio.CancelledError


async def _finish_close(resources):
    from gateway.run_runtime import _retire_profile_authority, ProfileOwnershipRetained
    from gateway.session_protected_process import retry_protected_process_closes
    authority, control, runner, db, ownership, home = resources
    if authority is not None:
        if not await _retire_profile_authority(authority):
            raise ProfileOwnershipRetained('card owner work did not retire; reservation retained')
        await retry_protected_process_closes(authority)
        for pending in tuple(getattr(authority, '_protected_retirements', {}).values()):
            await asyncio.to_thread(pending.close)
        if getattr(authority, '_protected_retirements', {}) or getattr(authority, '_protected_process_closes', {}):
            raise ProfileOwnershipRetained('protected card resources remain owned')
    if control is not None:
        await control.stop()
    tickets = getattr(runner, 'session_ticket_store', None)
    if tickets is not None:
        tickets.revoke()
    if db is not None:
        db.close()
    _owned_card_authorities.pop(str(home), None)
    ownership.close()
    _retained_card_resources.pop(str(home), None)
