"""Inbound Telegram media survives a network blip instead of asking the user to resend.

A gateway log showed ``msg.photo[-1].get_file()`` failing once with ``httpx.ConnectError`` while the
transport flapped for a few seconds; the user was told to resend. These tests inject that error.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import anyio
import httpx
import pytest
from telegram.error import BadRequest, NetworkError  # tests/gateway/conftest.py mirrors PTB's hierarchy

from plugins.platforms.telegram import telegram_media_fetch as fetch_mod
from plugins.platforms.telegram.telegram_media_fetch import fetch_telegram_file
from plugins.platforms.telegram.telegram_network import describe_cause_chain
from tests.gateway.test_telegram_documents import _make_file_obj, _make_message, _make_update


def _ptb_connect_error() -> NetworkError:
    """The exact shape PTB raised in the incident: NetworkError <- httpx.ConnectError('') <- EndOfStream."""
    try:
        try:
            raise anyio.EndOfStream()
        except anyio.EndOfStream as eos:
            raise httpx.ConnectError("") from eos
    except httpx.ConnectError as err:
        exc = NetworkError(f"httpx.ConnectError: {err}")
        exc.__cause__ = err
        return exc


class _FakeClock:
    """Drives both the backoff sleeps and the retry window, so no test waits on the wall clock."""

    def __init__(self):
        self.now = 0.0
        self.sleeps: list[float] = []

    async def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay

    def time(self):
        return self.now


@pytest.fixture()
def clock(monkeypatch):
    fake = _FakeClock()
    monkeypatch.setattr(fetch_mod, "_sleep", fake.sleep)
    monkeypatch.setattr(fetch_mod, "_clock", fake.time)
    return fake


@pytest.mark.asyncio
async def test_get_file_connect_error_is_retried_with_backoff(clock):
    source = MagicMock()
    source.get_file = AsyncMock(side_effect=[_ptb_connect_error(), _ptb_connect_error(), _make_file_obj(b"image")])

    _, data = await fetch_telegram_file(source, "photo")

    assert data == b"image"
    assert source.get_file.await_count == 3
    assert clock.sleeps == list(fetch_mod.FOREGROUND_RETRY_DELAYS_S[:2])


@pytest.mark.asyncio
async def test_a_failed_download_is_retried_too(clock):
    """The second leg (file download) fails the same way and gets a fresh getFile on retry."""
    flaky = _make_file_obj(b"image")
    flaky.download_as_bytearray = AsyncMock(side_effect=[_ptb_connect_error(), bytearray(b"image")])
    source = MagicMock()
    source.get_file = AsyncMock(return_value=flaky)

    _, data = await fetch_telegram_file(source, "photo")

    assert data == b"image"
    assert source.get_file.await_count == 2


@pytest.mark.asyncio
async def test_permanent_errors_are_not_retried(clock):
    source = MagicMock()
    source.get_file = AsyncMock(side_effect=BadRequest("File is too big"))
    with pytest.raises(BadRequest):
        await fetch_telegram_file(source, "photo")
    assert source.get_file.await_count == 1
    assert clock.sleeps == []


def test_cause_chain_names_the_tls_layer():
    assert describe_cause_chain(_ptb_connect_error()) == (
        "NetworkError('httpx.ConnectError: ') <- ConnectError <- EndOfStream")


@pytest.fixture()
def adapter(tmp_path, monkeypatch):
    from gateway.config import PlatformConfig
    from plugins.platforms.telegram.adapter import TelegramAdapter

    a = TelegramAdapter(PlatformConfig(enabled=True, token="fake-token"))
    a.handle_message = AsyncMock()
    a._route_photo_event = AsyncMock()  # the photo-burst batcher is out of scope here
    a._is_callback_user_authorized = lambda user_id, **_kw: True
    cached = tmp_path / "photo.jpg"
    monkeypatch.setattr("plugins.platforms.telegram.adapter.cache_image_from_bytes_async",
                        AsyncMock(return_value=str(cached)))
    return a


def _photo_message(get_file: AsyncMock):
    photo = MagicMock()
    photo.get_file = get_file
    msg = _make_message(photo=[photo])
    msg.reply_to_message = msg.quote = None
    return msg


async def _drain_retries(adapter):
    for task in list(adapter._media_retry_tasks_set()):
        await task


@pytest.mark.asyncio
async def test_photo_survives_a_short_blip_without_a_resend_notice(adapter, clock):
    get_file = AsyncMock(side_effect=[_ptb_connect_error(), _ptb_connect_error(), _make_file_obj(b"img")])
    msg = _photo_message(get_file)

    await adapter._handle_media_message(_make_update(msg), SimpleNamespace())

    adapter._route_photo_event.assert_awaited_once()
    msg.reply_text.assert_not_awaited()
    assert adapter._media_retry_tasks_set() == set()


@pytest.mark.asyncio
async def test_photo_still_failing_is_handed_to_the_session_by_the_background_retry(adapter, clock):
    tries = len(fetch_mod.FOREGROUND_RETRY_DELAYS_S) + 1
    get_file = AsyncMock(side_effect=[_ptb_connect_error()] * tries + [_make_file_obj(b"img")])
    msg = _photo_message(get_file)

    await adapter._handle_media_message(_make_update(msg), SimpleNamespace())

    # Deferred: nothing dispatched and nobody told to resend while the background retry owns it.
    adapter._route_photo_event.assert_not_awaited()
    adapter.handle_message.assert_not_awaited()
    msg.reply_text.assert_not_awaited()

    await _drain_retries(adapter)

    adapter._route_photo_event.assert_awaited_once()
    event = adapter._route_photo_event.await_args.args[1]
    assert event.media_urls and "could not be downloaded" not in (event.text or "")
    msg.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_user_is_told_only_after_the_background_window_closes(adapter, clock):
    def always_down(**_timeouts):
        raise _ptb_connect_error()

    get_file = AsyncMock(side_effect=always_down)
    msg = _photo_message(get_file)

    await adapter._handle_media_message(_make_update(msg), SimpleNamespace())
    msg.reply_text.assert_not_awaited()
    await _drain_retries(adapter)

    msg.reply_text.assert_awaited_once()
    adapter.handle_message.assert_awaited_once()
    event = adapter.handle_message.await_args.args[0]
    assert "could not be downloaded" in event.text
    tries = len(fetch_mod.FOREGROUND_RETRY_DELAYS_S) + 1
    assert get_file.await_count > 2 * tries  # the background passes really retried
    background_sleeps = [s for s in clock.sleeps if s >= fetch_mod.BACKGROUND_FIRST_DELAY_S]
    assert sum(background_sleeps) <= fetch_mod.BACKGROUND_RETRY_WINDOW_S


@pytest.mark.asyncio
@pytest.mark.parametrize("task_started", [False, True], ids=["cancelled-before-first-step", "waiting-between-passes"])
async def test_disconnect_cancels_a_pending_background_retry(adapter, monkeypatch, task_started):
    import asyncio

    gate = asyncio.Event()
    waiting = asyncio.Event()

    async def blocked_sleep(_delay):
        waiting.set()
        await gate.wait()

    monkeypatch.setattr(fetch_mod, "_sleep", blocked_sleep)
    monkeypatch.setattr(fetch_mod, "FOREGROUND_RETRY_DELAYS_S", ())
    msg = _photo_message(AsyncMock(side_effect=_ptb_connect_error()))

    await adapter._handle_media_message(_make_update(msg), SimpleNamespace())
    (task,) = adapter._media_retry_tasks_set()
    if task_started:
        await asyncio.wait_for(waiting.wait(), 5)

    await adapter._cancel_media_retries()

    assert task.cancelled()
    adapter.handle_message.assert_not_awaited()  # the gateway is going down: no new turn
    # ...but the message is not dropped silently: the user is told the photo did not arrive.
    msg.reply_text.assert_awaited_once()
    assert "photo" in msg.reply_text.await_args.args[0].lower()


@pytest.mark.asyncio
async def test_disconnect_notice_is_bounded_when_the_network_is_down(adapter, monkeypatch):
    import asyncio

    gate = asyncio.Event()

    async def blocked_sleep(_delay):
        await gate.wait()

    async def reply_hangs(*_a, **_kw):
        await asyncio.Event().wait()

    monkeypatch.setattr(fetch_mod, "_sleep", blocked_sleep)
    monkeypatch.setattr(fetch_mod, "FOREGROUND_RETRY_DELAYS_S", ())
    monkeypatch.setattr(fetch_mod, "CANCEL_NOTICE_TIMEOUT_S", 0.05)
    msg = _photo_message(AsyncMock(side_effect=_ptb_connect_error()))
    msg.reply_text = AsyncMock(side_effect=reply_hangs)
    await adapter._handle_media_message(_make_update(msg), SimpleNamespace())
    (task,) = adapter._media_retry_tasks_set()

    await asyncio.wait_for(adapter._cancel_media_retries(), 5)

    assert task.cancelled()
    msg.reply_text.assert_awaited_once()


@pytest.mark.asyncio
async def test_stalled_connection_stays_inside_the_foreground_budget(clock):
    """A server that accepts and never answers: every call runs to its timeout. The whole fetch
    must still end within the budget (the reviewer's real-PTB repro took 100 s before the cap)."""
    from telegram.error import TimedOut

    timeouts: list[dict] = []

    async def stalls(**kw):
        timeouts.append(kw)
        clock.now += kw["read_timeout"]
        raise TimedOut("Timed out")

    source = MagicMock()
    source.get_file = AsyncMock(side_effect=stalls)

    with pytest.raises(TimedOut) as raised:
        await fetch_telegram_file(source, "photo")

    assert clock.now <= fetch_mod.FOREGROUND_BUDGET_S
    assert all(kw["read_timeout"] <= fetch_mod.FOREGROUND_CALL_TIMEOUT_S for kw in timeouts)
    assert getattr(raised.value, fetch_mod._GAVE_UP_ATTR)  # handed to the background stage


@pytest.mark.asyncio
async def test_the_last_try_gets_only_what_is_left_of_the_budget(clock, monkeypatch):
    from telegram.error import TimedOut

    monkeypatch.setattr(fetch_mod, "FOREGROUND_BUDGET_S", 25.0)
    calls: list[dict] = []

    async def fast_fail_then_stall(**kw):
        calls.append(kw)
        if len(calls) < 4:
            raise _ptb_connect_error()
        clock.now += kw["read_timeout"]
        raise TimedOut("Timed out")

    source = MagicMock()
    source.get_file = AsyncMock(side_effect=fast_fail_then_stall)

    with pytest.raises(TimedOut):
        await fetch_telegram_file(source, "photo")

    assert len(calls) == 4
    assert calls[-1]["read_timeout"] == pytest.approx(5.0)  # 25 s budget, 20 s already spent
    assert clock.now <= 25.0


@pytest.mark.parametrize(("make", "retryable"), [
    (lambda e: _ptb_connect_error(), True),
    (lambda e: e.TimedOut("Timed out"), True),
    (lambda e: e.NetworkError("Internal Server Error (500)"), True),
    (lambda e: e.NetworkError("Bad Gateway"), True),  # Telegram's description for a 502
    (lambda e: e.NetworkError("Request Entity Too Large (413)"), False),
    (lambda e: e.NetworkError("Unknown error in HTTP implementation: ValueError()"), False),
    (lambda e: e.BadRequest("Wrong file_id or the file is temporarily unavailable"), False),
    (lambda e: e.Forbidden("Forbidden: bot was blocked by the user"), False),
])
def test_only_transient_errors_are_retryable(make, retryable):
    import telegram.error as ptb_error

    assert fetch_mod.is_retryable_download_error(make(ptb_error)) is retryable


def test_cause_chain_falls_back_to_the_type_name_when_str_raises():
    class Unprintable(Exception):
        def __str__(self):
            raise RuntimeError("boom")

    assert describe_cause_chain(Unprintable()) == "Unprintable"


@pytest.mark.asyncio
async def test_a_bad_file_id_tells_the_user_at_once(adapter, clock):
    msg = _photo_message(AsyncMock(side_effect=BadRequest("Wrong file_id")))

    await adapter._handle_media_message(_make_update(msg), SimpleNamespace())

    assert adapter._media_retry_tasks_set() == set()
    assert clock.sleeps == []
    msg.reply_text.assert_awaited_once()
    adapter.handle_message.assert_awaited_once()
    assert "could not be downloaded" in adapter.handle_message.await_args.args[0].text


@pytest.mark.asyncio
async def test_past_the_pending_cap_a_failure_is_reported_at_once(adapter, clock):
    busy = adapter._media_retry_tasks_set()
    busy.update(object() for _ in range(fetch_mod.BACKGROUND_MAX_PENDING))  # only the count matters
    msg = _photo_message(AsyncMock(side_effect=_ptb_connect_error()))

    await adapter._handle_media_message(_make_update(msg), SimpleNamespace())

    assert len(adapter._media_retry_tasks_set()) == fetch_mod.BACKGROUND_MAX_PENDING  # no new task
    msg.reply_text.assert_awaited_once()
    adapter.handle_message.assert_awaited_once()
