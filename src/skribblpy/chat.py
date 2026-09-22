"""One bounded, cancellable FIFO for chat and guesses."""

from time import monotonic
from typing import Optional
from dataclasses import dataclass
from skribblpy.models import SendReceipt
from collections.abc import Callable, Awaitable
from skribblpy._callbacks import is_async_callable
from skribblpy.errors import QueueFull, ActionError, Disconnected
from skribblpy.constants import MAX_CHAT_LENGTH, MIN_CHAT_INTERVAL
from asyncio import (
    Task,
    Queue,
    sleep,
    Future,
    gather,
    QueueEmpty,
    create_task,
    current_task,
    CancelledError,
    get_running_loop,
)

type TextSource = str | Callable[[], Awaitable[str]]


@dataclass(slots=True)
class _Request:
    text: TextSource
    guess: bool
    turn_id: int
    future: Future[SendReceipt]
    queued: bool


def validate_text(text: str):
    if not isinstance(text, str) or not 1 <= len(text) <= MAX_CHAT_LENGTH:
        raise ValueError('Chat must contain 1..100 characters')


class ChatSender:
    def __init__(
        self,
        client,
        *,
        send: Callable[[str, bool, int], Awaitable[SendReceipt]],
        publish: Callable[[str, object], None],
        interval: float,
        backoff: float,
        capacity: int,
    ):
        self.client = client
        self._send_chat = send
        self._publish = publish
        self.interval = max(MIN_CHAT_INTERVAL, interval)
        self.backoff = backoff or 2 * self.interval
        self.queue: Queue[_Request] = Queue(capacity)
        self.task: Optional[Task[None]] = None
        self.closed = False
        self._next_send = 0.0
        self._backoff_until = 0.0

    def start(self):
        self.task = create_task(self._run(), name='skribbl-chat')

    def enqueue(
        self, text: TextSource, *, guess: bool = False, queued: bool = False
    ) -> Future[SendReceipt]:
        if self.closed or not self.client.connected:
            raise Disconnected('Chat sender is closed')
        if callable(text):
            if not is_async_callable(text):
                raise TypeError('Deferred text builder must be an async callable; use async def')
        else:
            validate_text(text)
        if self.queue.full():
            raise QueueFull('Chat queue is full')
        future = get_running_loop().create_future()
        if queued:
            # Event-only users need not consume the returned future's exception.
            future.add_done_callback(
                lambda done: done.exception() if not done.cancelled() else None
            )
        self.queue.put_nowait(_Request(text, guess, self.client.snapshot.turn_id, future, queued))
        return future

    def note_spam(self):
        self._backoff_until = max(self._backoff_until, monotonic() + self.backoff)

    async def close(self, *, flush: bool):
        self.closed = True
        if self.task is not None:
            if flush:
                await self.queue.join()
            self.task.cancel()
            await gather(self.task, return_exceptions=True)
        while True:
            try:
                request = self.queue.get_nowait()
            except QueueEmpty:
                break
            request.future.cancel()
            self.queue.task_done()

    async def _run(self):
        while True:
            request = await self.queue.get()
            work = create_task(self._send(request))

            def cancel_work(future, active=work):
                if future.cancelled():
                    active.cancel()

            request.future.add_done_callback(cancel_work)
            result: Optional[SendReceipt | Exception] = None
            try:
                receipt = await work
                if not request.future.done():
                    request.future.set_result(receipt)
                result = receipt
            except CancelledError:
                request.future.cancel()
                task = current_task()
                if task is not None and task.cancelling():
                    raise
            except Exception as error:
                if not request.future.done():
                    request.future.set_exception(error)
                result = error
            finally:
                request.future.remove_done_callback(cancel_work)
                self.queue.task_done()
            if request.queued and result is not None:
                try:
                    self._publish('queued_chat', result)
                except QueueFull:
                    return

    async def _send(self, request: _Request) -> SendReceipt:
        await self._wait_for_slot(request)
        text = await request.text() if callable(request.text) else request.text
        validate_text(text)
        # A deferred builder may have awaited while a new spam warning arrived.
        await self._wait_for_slot(request)
        state = self.client.snapshot
        if request.guess and (not state.guessing_enabled or state.turn_id != request.turn_id):
            raise ActionError('Guess belongs to a finished or unavailable turn')
        receipt = await self._send_chat(text, request.guess, state.turn_id)
        self._next_send = monotonic() + self.interval
        return receipt

    async def _wait_for_slot(self, request: _Request):
        while True:
            if request.future.cancelled():
                raise CancelledError
            delay = max(self._next_send, self._backoff_until) - monotonic()
            if delay <= 0:
                return
            await sleep(delay)
