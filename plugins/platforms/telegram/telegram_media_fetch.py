"""Inbound Telegram media download that survives network blips.

A ``getFile`` + file download is a read the user cannot repeat for us: when it fails the user
used to be told to resend. On this host the Telegram transport flaps for a few seconds at a
time (both the IPv4 literal and the dual-stack hostname drop the TLS handshake together, then
recover), so one try lost photos that a retry seconds later would have fetched.

Two layers:

* :func:`fetch_telegram_file` retries a retryable network error in the foreground with backoff
  (4 tries over about 30 s) before giving up. It is used by every inbound download path.
* :class:`TelegramMediaRetryMixin` takes over when the foreground retries are spent: it keeps
  the update (and so the ``file_id``) and re-runs the media handler in the background for up to
  10 minutes, so the image reaches the session without a resend. Only the last background pass
  tells the user it failed.
"""

from __future__ import annotations

import asyncio
import logging
import time
from contextvars import ContextVar
from typing import Any, Literal

# The cause chain says whether TCP (``OSError``) or the TLS handshake (``EndOfStream``) failed.
from plugins.platforms.telegram.telegram_network import describe_cause_chain

logger = logging.getLogger("plugins.platforms.telegram.adapter")

#: Sleeps between the foreground tries: 4 tries, about 30 s with the requests themselves.
FOREGROUND_RETRY_DELAYS_S: tuple[float, ...] = (2.0, 6.0, 12.0)
#: How long a deferred download keeps retrying in the background, measured from deferral.
BACKGROUND_RETRY_WINDOW_S = 600.0
#: First background wait, doubled per pass up to the cap.
BACKGROUND_FIRST_DELAY_S = 30.0
BACKGROUND_MAX_DELAY_S = 120.0
#: Deferred downloads one adapter keeps at once; past it a failure is reported at once, so a long
#: outage with many photos cannot pile up unbounded tasks.
BACKGROUND_MAX_PENDING = 32

# Marks the exception that ended the foreground retries of a download, so only a download failure
# (never an unrelated dispatch error inside the same try block) can defer the update.
_GAVE_UP_ATTR = "_hermes_media_fetch_retryable"

# Which pass the media handler runs in. Unset on the live PTB pass; a background task sets it
# for its own copy of the context only. "retry": a retryable failure raises MediaRetryPending
# instead of telling the user; "final": the last pass reports a failure as the live path did.
RetryPass = Literal["retry", "final"]
_RETRY_PASS: ContextVar[RetryPass | None] = ContextVar("telegram_media_retry_pass", default=None)

# Module seams so tests drive the backoff and the retry window without wall-clock waits.
_sleep = asyncio.sleep
_clock = time.monotonic


class MediaRetryPending(Exception):
    """A media download failed retryably and the background retry owns the update now."""


def is_retryable_download_error(exc: BaseException) -> bool:
    """True for a transient PTB network error (connect/TLS failure, timeout). ``BadRequest``
    subclasses ``NetworkError`` but is permanent (file too big, bad file_id), so it never retries."""
    try:
        from telegram.error import BadRequest, NetworkError
    except ImportError:  # SDK missing: nothing PTB-specific can be retryable
        return False
    return isinstance(exc, NetworkError) and not isinstance(exc, BadRequest)


async def fetch_telegram_file(source: Any, what: str) -> tuple[Any, bytes]:
    """``source.get_file()`` + download, retried on a transient network error.

    Each try calls ``getFile`` again, so a retry also refreshes the file path. Returns
    ``(file_obj, data)``. The last error is re-raised; when it is retryable it is marked so
    :meth:`TelegramMediaRetryMixin._defer_media_failure` may hand the update to the background.
    """
    tries = len(FOREGROUND_RETRY_DELAYS_S) + 1
    for attempt in range(1, tries + 1):
        try:
            file_obj = await source.get_file()
            data = await file_obj.download_as_bytearray()
            if attempt > 1:
                logger.info("[Telegram] %s download succeeded on try %d/%d", what, attempt, tries)
            return file_obj, bytes(data)
        except Exception as exc:
            if not is_retryable_download_error(exc):
                raise
            if attempt == tries:
                setattr(exc, _GAVE_UP_ATTR, True)
                raise
            delay = FOREGROUND_RETRY_DELAYS_S[attempt - 1]
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
        except MediaRetryPending:
            # The background task owns the update now: PTB must not replay it.
            self._accept_update()
            self._start_media_retry(update, context)

    def _defer_media_failure(self, exc: BaseException) -> None:
        """Called first thing when a media download failed: raise :class:`MediaRetryPending` when
        the update should be retried in the background instead of telling the user now."""
        if not getattr(exc, _GAVE_UP_ATTR, False):
            return
        retry_pass = _RETRY_PASS.get()
        if retry_pass == "final":
            return
        if retry_pass is None and len(self._media_retry_tasks_set()) >= BACKGROUND_MAX_PENDING:
            logger.warning("[Telegram] %d media downloads already retrying; reporting this failure now",
                           BACKGROUND_MAX_PENDING)
            return
        raise MediaRetryPending() from exc

    def _media_retry_tasks_set(self) -> set[asyncio.Task]:
        # Lazily created: tests build adapters with object.__new__, skipping __init__.
        return self.__dict__.setdefault("_media_retry_tasks", set())

    def _start_media_retry(self, update: Any, context: Any) -> None:
        tasks = self._media_retry_tasks_set()
        task = asyncio.get_running_loop().create_task(
            self._retry_media_update(update, context), name="telegram-media-retry")
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        logger.warning(
            "[Telegram] Media download for update %s deferred; retrying in the background for up to %.0fs",
            getattr(update, "update_id", "?"), BACKGROUND_RETRY_WINDOW_S)

    async def _retry_media_update(self, update: Any, context: Any) -> None:
        """Re-run the media handler until the download succeeds or the window closes; the pass
        whose next wait would end past the window is the final one and reports a failure."""
        admission = getattr(self, "_update_admission", None)
        if admission is not None:
            admission.set(None)  # the PTB claim was handed off and released; never touch it again
        deadline = _clock() + BACKGROUND_RETRY_WINDOW_S
        delay = BACKGROUND_FIRST_DELAY_S
        update_id = getattr(update, "update_id", "?")
        try:
            while True:
                await _sleep(delay)
                delay = min(delay * 2, BACKGROUND_MAX_DELAY_S)
                final = _clock() + delay >= deadline
                _RETRY_PASS.set("final" if final else "retry")
                try:
                    await self._process_media_message(update, context)
                except MediaRetryPending:
                    logger.warning("[Telegram] Background media retry for update %s still failing; next pass in %.0fs",
                                   update_id, delay)
                    continue
                logger.info("[Telegram] Background media retry for update %s finished", update_id)
                return
        except asyncio.CancelledError:
            logger.warning("[Telegram] Background media retry for update %s cancelled by disconnect; the attachment is lost",
                           update_id)
            raise
        except Exception:
            logger.exception("[Telegram] Background media retry for update %s failed", update_id)

    async def _cancel_media_retries(self) -> None:
        """Disconnect: a retry must not run the handler against a torn-down bot client."""
        tasks = list(self._media_retry_tasks_set())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
