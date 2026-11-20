"""Inbound Telegram media download that survives network blips.

A ``getFile`` + file download is a read the user cannot repeat for us: when it fails the user
used to be told to resend. On this host the Telegram transport flaps for a few seconds at a
time (both the IPv4 literal and the dual-stack hostname drop the TLS handshake together, then
recover), so one try lost photos that a retry seconds later would have fetched.

Two layers:

* :func:`fetch_telegram_file` retries a transient error in the foreground with backoff inside a
  total budget of :data:`FOREGROUND_BUDGET_S` (about 30 s, a stalled connection included: every
  call gets PTB per-call timeouts capped by what is left of the budget). It runs inside the
  chat's in-order queue, so this budget is what bounds how long that chat waits. Every inbound
  download path uses it.
* :class:`TelegramMediaRetryMixin` takes over when the foreground budget is spent: it keeps the
  update (and so the ``file_id``) and re-runs the media handler in its own task for up to 10
  minutes, so the image reaches the session without a resend and without holding the chat. Only
  the last background pass tells the user it failed; a disconnect inside the window tells the
  user at once (best effort, bounded) instead of dropping the message silently.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from contextvars import ContextVar
from dataclasses import dataclass
from http import HTTPStatus
from typing import Any, Awaitable, Callable, Literal

# The cause chain says whether TCP (``OSError``) or the TLS handshake (``EndOfStream``) failed.
from plugins.platforms.telegram.telegram_network import describe_cause_chain

logger = logging.getLogger("plugins.platforms.telegram.adapter")

#: Total wall time one foreground fetch may take, tries and backoff included.
FOREGROUND_BUDGET_S = 30.0
#: Sleeps between the foreground tries (4 tries fit the budget when each fails fast, e.g. a
#: dropped TLS handshake; fewer when a try stalls until its timeout).
FOREGROUND_RETRY_DELAYS_S: tuple[float, ...] = (2.0, 6.0, 12.0)
#: Cap on each call's connect/read/write/pool timeout, so one stalled call cannot eat the budget.
#: httpx applies the read timeout per socket read, so a download that keeps receiving bytes is
#: never cut by it; only a silent connection is.
FOREGROUND_CALL_TIMEOUT_S = 10.0
#: A try is not started with less budget than this left: it could not finish a real exchange.
FOREGROUND_MIN_TRY_S = 3.0
#: How long a deferred download keeps retrying in the background, measured from deferral.
BACKGROUND_RETRY_WINDOW_S = 600.0
#: First background wait, doubled per pass up to the cap.
BACKGROUND_FIRST_DELAY_S = 30.0
BACKGROUND_MAX_DELAY_S = 120.0
#: Deferred downloads one adapter keeps at once; past it a failure is reported at once, so a long
#: outage with many photos cannot pile up unbounded tasks.
BACKGROUND_MAX_PENDING = 32
#: Time a disconnect gives the "could not download" reply of a deferred download. It stays under
#: the adapter's per-step disconnect timeout (2 s), which would otherwise cancel the reply too.
CANCEL_NOTICE_TIMEOUT_S = 1.5

# Marks the exception that ended the foreground retries of a download, so only a download failure
# (never an unrelated dispatch error inside the same try block) can defer the update.
_GAVE_UP_ATTR = "_hermes_media_fetch_retryable"

# Which pass the media handler runs in. Unset on the live PTB pass; a background task sets it
# around one pass and resets it after. "retry": a retryable failure raises MediaRetryPending
# instead of telling the user; "final": the last pass reports a failure as the live path did.
RetryPass = Literal["retry", "final"]
_RETRY_PASS: ContextVar[RetryPass | None] = ContextVar("telegram_media_retry_pass", default=None)

# PTB keeps no status code on the NetworkError it raises for an HTTP status it does not map: the
# message is "<phrase> (<code>)", or Telegram's description, which for a 5xx is the status phrase.
_5XX_CODE = re.compile(r"\(5\d\d\)")
_5XX_PHRASES = tuple(s.phrase for s in HTTPStatus if 500 <= s.value <= 599)

# Module seams so tests drive the backoff and the budgets without wall-clock waits.
_sleep = asyncio.sleep
_clock = time.monotonic

#: Sends the user the "could not download, please resend" reply for one deferred download.
FailureNotifier = Callable[[], Awaitable[Any]]


class MediaRetryPending(Exception):
    """A media download failed retryably and the background retry owns the update now.

    ``notify`` tells the user it failed; a disconnect that ends the retry window early calls it,
    so the message is never dropped silently.
    """

    def __init__(self, notify: FailureNotifier | None = None):
        super().__init__("media download deferred to the background retry")
        self.notify = notify


@dataclass
class _PendingMediaRetry:
    """One deferred download, kept beside its task so a disconnect can still tell the user.

    The notice is sent by :meth:`TelegramMediaRetryMixin._cancel_media_retries`, not by the task:
    a task cancelled before its first step never runs its body, so a handler inside it would miss
    exactly the retries started just before a disconnect.
    """

    update_id: Any
    #: The newest pass's reply (it names the freshest error); a pass replaces it.
    notify: FailureNotifier | None


def _caused_by_transport_error(exc: BaseException) -> bool:
    try:
        import httpx
    except ImportError:  # PTB always ships httpx; without it nothing is provably a transport error
        return False
    current: BaseException | None = exc
    for _ in range(6):  # bounded: a cyclic chain must not loop
        if current is None:
            return False
        if isinstance(current, httpx.TransportError):
            return True
        current = current.__cause__ or current.__context__
    return False


def is_retryable_download_error(exc: BaseException) -> bool:
    """True only for a transient failure: a timeout, a transport error (connect, TLS, reset) or a 5xx.

    PTB raises ``NetworkError`` for those, but also for a 4xx it does not map (e.g. 413) and for an
    unknown client error, and ``BadRequest`` (a bad or expired file_id) subclasses it: none of
    those is retried.
    """
    try:
        from telegram.error import BadRequest, NetworkError, TimedOut
    except ImportError:  # SDK missing: nothing PTB-specific can be retryable
        return False
    if isinstance(exc, TimedOut):
        return True
    if not isinstance(exc, NetworkError) or isinstance(exc, BadRequest):
        return False
    if _caused_by_transport_error(exc):
        return True
    text = str(exc)
    return bool(_5XX_CODE.search(text)) or text.startswith(_5XX_PHRASES)


def _call_timeouts(deadline: float) -> dict[str, float]:
    """PTB per-call timeouts: :data:`FOREGROUND_CALL_TIMEOUT_S`, or what is left of the budget if less."""
    value = min(FOREGROUND_CALL_TIMEOUT_S, max(deadline - _clock(), 0.1))
    return {"connect_timeout": value, "read_timeout": value, "write_timeout": value, "pool_timeout": value}


async def fetch_telegram_file(source: Any, what: str) -> tuple[Any, bytes]:
    """``source.get_file()`` + download, retried on a transient error within :data:`FOREGROUND_BUDGET_S`.

    Each try calls ``getFile`` again, so a retry also refreshes the file path. Returns
    ``(file_obj, data)``. The last error is re-raised; when it is retryable it is marked so
    :meth:`TelegramMediaRetryMixin._defer_media_failure` may hand the update to the background.
    """
    deadline = _clock() + FOREGROUND_BUDGET_S
    tries = len(FOREGROUND_RETRY_DELAYS_S) + 1
    for attempt in range(1, tries + 1):
        try:
            file_obj = await source.get_file(**_call_timeouts(deadline))
            data = await file_obj.download_as_bytearray(**_call_timeouts(deadline))
            if attempt > 1:
                logger.info("[Telegram] %s download succeeded on try %d/%d", what, attempt, tries)
            return file_obj, bytes(data)
        except Exception as exc:
            if not is_retryable_download_error(exc):
                raise
            delay = FOREGROUND_RETRY_DELAYS_S[attempt - 1] if attempt < tries else 0.0
            if attempt == tries or _clock() + delay + FOREGROUND_MIN_TRY_S > deadline:
                logger.warning("[Telegram] %s download try %d/%d failed (%s); foreground budget spent",
                               what, attempt, tries, describe_cause_chain(exc))
                setattr(exc, _GAVE_UP_ATTR, True)
                raise
            logger.warning(
                "[Telegram] %s download try %d/%d failed (%s); retrying in %.0fs",
                what, attempt, tries, describe_cause_chain(exc), delay)
            await _sleep(delay)
    raise AssertionError("unreachable: the last try returns or raises")


class TelegramMediaRetryMixin:
    """Background retry of an inbound media update whose download failed retryably.

    The adapter's media handler is ``_process_media_message``; PTB calls ``_handle_media_message``
    (here), which runs it and, when the download is deferred, schedules the re-run.
    """

    async def _handle_media_message(self, update: Any, context: Any) -> None:
        try:
            await self._process_media_message(update, context)
        except MediaRetryPending as pending:
            # The background task owns the update now: PTB must not replay it.
            self._accept_update()
            self._start_media_retry(update, context, pending.notify)

    def _defer_media_failure(self, exc: BaseException, notify: FailureNotifier | None = None) -> None:
        """Called first thing when a media download failed: raise :class:`MediaRetryPending` when
        the update should be retried in the background instead of telling the user now. ``notify``
        is the reply the background task sends if a disconnect ends its window early."""
        if not getattr(exc, _GAVE_UP_ATTR, False):
            return
        retry_pass = _RETRY_PASS.get()
        if retry_pass == "final":
            return
        if retry_pass is None and len(self._media_retry_tasks_set()) >= BACKGROUND_MAX_PENDING:
            logger.warning("[Telegram] %d media downloads already retrying; reporting this failure now",
                           BACKGROUND_MAX_PENDING)
            return
        raise MediaRetryPending(notify) from exc

    def _media_retry_tasks_set(self) -> set[asyncio.Task]:
        # Lazily created: tests build adapters with object.__new__, skipping __init__.
        return self.__dict__.setdefault("_media_retry_tasks", set())

    def _media_retry_pending_map(self) -> dict[asyncio.Task, _PendingMediaRetry]:
        # Same lazy creation; an entry lives exactly as long as its task.
        return self.__dict__.setdefault("_media_retry_pending", {})

    def _start_media_retry(self, update: Any, context: Any, notify: FailureNotifier | None) -> None:
        tasks = self._media_retry_tasks_set()
        pending_map = self._media_retry_pending_map()
        pending = _PendingMediaRetry(update_id=getattr(update, "update_id", "?"), notify=notify)
        task = asyncio.get_running_loop().create_task(
            self._retry_media_update(update, context, pending), name="telegram-media-retry")
        tasks.add(task)
        pending_map[task] = pending

        def _forget(done: asyncio.Task) -> None:
            tasks.discard(done)
            pending_map.pop(done, None)

        task.add_done_callback(_forget)
        logger.warning(
            "[Telegram] Media download for update %s deferred; retrying in the background for up to %.0fs",
            pending.update_id, BACKGROUND_RETRY_WINDOW_S)

    async def _retry_media_update(self, update: Any, context: Any, pending: _PendingMediaRetry) -> None:
        """Re-run the media handler until the download succeeds or the window closes; the pass
        whose next wait would end past the window is the final one and reports a failure."""
        admission = getattr(self, "_update_admission", None)
        if admission is not None:
            admission.set(None)  # the PTB claim was handed off and released; never touch it again
        deadline = _clock() + BACKGROUND_RETRY_WINDOW_S
        delay = BACKGROUND_FIRST_DELAY_S
        update_id = pending.update_id
        try:
            while True:
                await _sleep(delay)
                delay = min(delay * 2, BACKGROUND_MAX_DELAY_S)
                final = _clock() + delay >= deadline
                token = _RETRY_PASS.set("final" if final else "retry")
                try:
                    await self._process_media_message(update, context)
                except MediaRetryPending as still_failing:
                    pending.notify = still_failing.notify or pending.notify  # the newest pass knows the freshest error
                    logger.warning("[Telegram] Background media retry for update %s still failing; next pass in %.0fs",
                                   update_id, delay)
                    continue
                finally:
                    # Only this pass is a retry pass; tasks it spawned already copied the context.
                    _RETRY_PASS.reset(token)
                logger.info("[Telegram] Background media retry for update %s finished", update_id)
                return
        except Exception:
            logger.exception("[Telegram] Background media retry for update %s failed", update_id)

    @staticmethod
    async def _notify_cancelled_media_retry(pending: _PendingMediaRetry) -> None:
        """Best effort and bounded: the bot client is still up (media retries are cancelled before
        the updater stops), but a disconnect must never wait on a dead network."""
        if pending.notify is None:
            return
        logger.warning("[Telegram] Background media retry for update %s cancelled by disconnect; telling the user",
                       pending.update_id)
        try:
            await asyncio.wait_for(pending.notify(), CANCEL_NOTICE_TIMEOUT_S)
        except TimeoutError:  # the notifier logs its own reply errors; only the bound can end it
            logger.warning("[Telegram] Could not tell the user that update %s's attachment was lost: "
                           "no reply within %.1fs", pending.update_id, CANCEL_NOTICE_TIMEOUT_S)

    async def _cancel_media_retries(self) -> None:
        """Disconnect: a retry must not run the handler against a torn-down bot client, and every
        retry it ends tells its user the attachment was lost (the notices run concurrently, so
        the whole step stays within :data:`CANCEL_NOTICE_TIMEOUT_S` after the cancel)."""
        pending_map = self._media_retry_pending_map()
        tasks = list(pending_map)
        cut_short = [pending_map[task] for task in tasks]  # snapshot: done callbacks drop entries
        for task in tasks:
            task.cancel()
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)
        # A task that ended on its own (succeeded, or its final pass told the user) needs nothing.
        lost = [pending for task, pending in zip(tasks, cut_short) if task.cancelled()]
        results = await asyncio.gather(*(self._notify_cancelled_media_retry(p) for p in lost), return_exceptions=True)
        for pending, result in zip(lost, results):
            if isinstance(result, BaseException):  # one failed notice must not stop the others or the disconnect
                logger.warning("[Telegram] Could not tell the user that update %s's attachment was lost",
                               pending.update_id, exc_info=result)

    async def _reply_media_failure(
            self, msg: Any, event: Any, kind: str, exc: BaseException, display_name: str | None) -> tuple[str | None, str]:
        """Reply to the user that ``kind`` could not be downloaded. Returns ``(notice, named)``;
        ``notice`` is None when warnings are muted. Never raises: a failed reply is logged."""
        from plugins.platforms.telegram.adapter import _MEDIA_KIND_KEYS, t  # late: the adapter imports this module

        with self._media_delivery_scope(event.source):  # inbound media fails before handle_message binds the profile
            named = t("platform.telegram.media.named_suffix", name=display_name) if display_name else ""
            # ``kind`` is the English label the agent-visible note keeps; the chat reply is localized.
            notice = self.warning_text(t(
                "platform.telegram.media.download_failed",
                kind=t(_MEDIA_KIND_KEYS[kind]) if kind in _MEDIA_KIND_KEYS else kind,
                name=named, error=exc.__class__.__name__))
            if notice:
                try:
                    self._accept_update()
                    await msg.reply_text(notice)
                except Exception as reply_err:
                    logger.warning("[Telegram] Failed to notify user about %s cache failure: %s", kind, reply_err, exc_info=True)
        return notice, named
