from math import isfinite
from time import monotonic
from dataclasses import replace
from collections.abc import Iterable
from skribblpy.actions import Actions
from skribblpy.state import StateStore
from skribblpy.transport import Socket
from skribblpy.drawing import DrawCommand
from skribblpy.events import EventHandlers
from typing import Any, Optional, TypedDict
from aiohttp import ClientError, ClientSession
from skribblpy.chat import ChatSender, TextSource
from skribblpy.errors import QueueFull, Disconnected, JoinRejected, ProtocolError, ConnectionFailed
from skribblpy.models import (
    Event,
    Phase,
    freeze,
    PacketID,
    Snapshot,
    SendReceipt,
    GuessOutcome,
    GuessResponse,
    random_avatar,
)
from asyncio import (
    Lock,
    Task,
    Queue,
    Future,
    gather,
    shield,
    timeout,
    create_task,
    current_task,
    CancelledError,
    get_running_loop,
)
from skribblpy.constants import (
    MAX_NAME_LENGTH,
    DEFAULT_BASE_URL,
    MIN_CHAT_INTERVAL,
    AVATAR_EYE_VARIANTS,
    MAX_DRAWING_HISTORY,
    DEFAULT_EVENT_BUFFER,
    AVATAR_COLOR_VARIANTS,
    AVATAR_MOUTH_VARIANTS,
    DEFAULT_CHAT_QUEUE_SIZE,
    DEFAULT_CONNECT_TIMEOUT,
    ORDINARY_AVATAR_SPECIAL,
)


class _ChatOptions(TypedDict):
    interval: float
    backoff: float
    capacity: int


_MISSING = object()
_NAMES = {
    PacketID.PLAYER_JOIN: 'player_join',
    PacketID.PLAYER_LEAVE: 'player_leave',
    PacketID.VOTE_KICK: 'vote_kick',
    PacketID.VOTE: 'drawing_vote',
    PacketID.AVATAR: 'avatar',
    PacketID.LOBBY: 'lobby',
    PacketID.STATE: 'state',
    PacketID.SETTING: 'setting',
    PacketID.HINT: 'hint',
    PacketID.TIME: 'time',
    PacketID.GUESSED: 'guessed',
    PacketID.CLOSE_GUESS: 'close_guess',
    PacketID.OWNER: 'owner',
    PacketID.DRAW: 'draw',
    PacketID.CLEAR: 'clear',
    PacketID.UNDO: 'undo',
    PacketID.CHAT: 'chat',
    PacketID.START_ERROR: 'start_error',
    PacketID.SPAM: 'spam',
    PacketID.NAME: 'name',
}


class Client(Actions, EventHandlers):
    """Register handlers before ``await client.connect()``.

    ``connect`` waits for packet 10. ``wait_closed`` raises transport or handler
    errors. ``close`` cancels queued messages by default; pass ``flush=True``
    for a bounded graceful drain. One client belongs to one asyncio event loop.
    """

    def __init__(
        self,
        *,
        name: str = '',
        language: str = '0',
        avatar: Optional[Iterable[int]] = None,
        code: str = '',
        base_url: str = DEFAULT_BASE_URL,
        chat_interval: float = MIN_CHAT_INTERVAL,
        spam_backoff: float = 0,
        event_buffer: int = DEFAULT_EVENT_BUFFER,
        chat_queue_size: int = DEFAULT_CHAT_QUEUE_SIZE,
        track_drawing: bool = True,
        max_history: int = MAX_DRAWING_HISTORY,
        events: Optional[Iterable[str]] = None,
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT,
    ) -> None:
        super().__init__()
        if len(name) > MAX_NAME_LENGTH:
            raise ValueError('Name exceeds 21 characters')
        if any(not isfinite(value) or value < 0 for value in (chat_interval, spam_backoff)):
            raise ValueError('Chat timing must be finite and nonnegative')
        if not isfinite(connect_timeout) or connect_timeout <= 0:
            raise ValueError('Connection timeout must be finite and positive')
        if any(
            type(value) is not int or value < 1
            for value in (event_buffer, chat_queue_size, max_history)
        ):
            raise ValueError('Queue and history limits must be positive integers')
        avatar = tuple(avatar) if avatar is not None else random_avatar()
        if (
            len(avatar) != 4
            or any(type(value) is not int for value in avatar)
            or not 0 <= avatar[0] < AVATAR_COLOR_VARIANTS
            or not 0 <= avatar[1] < AVATAR_EYE_VARIANTS
            or not 0 <= avatar[2] < AVATAR_MOUTH_VARIANTS
            or avatar[3] < ORDINARY_AVATAR_SPECIAL
        ):
            raise ValueError('Invalid avatar')
        self.name = name if name.strip() else ''
        self.language = language
        self.avatar = avatar
        self.code = code
        self.base_url = base_url
        self.connect_timeout = connect_timeout
        self.events = frozenset(events) if events is not None else None
        self._store = StateStore(track_drawing=track_drawing, max_history=max_history)
        self._event_queue: Queue[Event] = Queue(event_buffer)
        self._chat_options = _ChatOptions(
            interval=chat_interval, backoff=spam_backoff, capacity=chat_queue_size
        )
        self._session: Optional[ClientSession] = None
        self._socket: Optional[Socket] = None
        self._chat: Optional[ChatSender] = None
        self._reader: Optional[Task[None]] = None
        self._dispatcher: Optional[Task[None]] = None
        self._done: Optional[Future[Optional[BaseException]]] = None
        self._ready: Optional[Future[Snapshot]] = None
        self._error: Optional[BaseException] = None
        self._closing = False
        self._cleanup_task: Optional[Task[None]] = None
        self._lifecycle = Lock()
        self._sequence = 0
        self._receipt_sequence = 0
        self._pending_guesses: list[SendReceipt] = []
        self._guess_outcomes: dict[int, str] = {}
        self._last_lobby = ''
        self.disconnect_reason = None

    @property
    def snapshot(self) -> Snapshot:
        """Latest authoritative state; event.snapshot preserves the event-time state."""
        return self._store.snapshot

    @property
    def connected(self) -> bool:
        """Whether the connection lifecycle is active; use snapshot.ready for lobby readiness."""
        return (
            self._socket is not None
            and not self._closing
            and not (self._done and self._done.done())
        )

    @property
    def drawing(self) -> tuple[DrawCommand, ...]:
        """Copy retained canvas commands; empty when drawing tracking is disabled."""
        return tuple(self._store.drawing)

    async def __aenter__(self) -> 'Client':
        return self

    async def __aexit__(self, *_) -> None:
        await self.close()

    async def connect(self, lobby_id: str = '', *, create_private: bool = False) -> Snapshot:
        """Make a fresh connection and return its authoritative lobby snapshot."""
        if create_private and lobby_id:
            raise ValueError('Private creation cannot specify an existing lobby')
        async with self._lifecycle:
            if self._session is not None:
                raise RuntimeError('Close the previous connection before connecting')
            self._closing = False
            self._cleanup_task = None
            self._error = None
            self.disconnect_reason = None
            self._store.reset()
            self._pending_guesses.clear()
            self._guess_outcomes.clear()
            self._done = get_running_loop().create_future()
            ready: Future[Snapshot] = get_running_loop().create_future()
            self._ready = ready
            ready.add_done_callback(lambda done: done.exception() if not done.cancelled() else None)
            session = ClientSession()
            self._session = session
            socket = Socket(session, base_url=self.base_url, connect_timeout=self.connect_timeout)
            self._socket = socket
            try:
                async with timeout(self.connect_timeout):
                    try:
                        await socket.connect(self.language, lobby_id)
                    except (ClientError, OSError) as error:
                        raise ConnectionFailed(f'Connection setup failed: {error}') from error
                    login = dict(
                        join=0 if create_private else lobby_id,
                        create=int(create_private),
                        name=self.name,
                        lang=self.language,
                        avatar=self.avatar,
                    )
                    if self.code:
                        login['code'] = self.code
                    await socket.emit('login', login)
                    chat = ChatSender(
                        self, send=self._send_chat, publish=self._publish, **self._chat_options
                    )
                    self._chat = chat
                    chat.start()
                    self._dispatcher = create_task(self._dispatch(), name='skribbl-events')
                    self._reader = create_task(self._read(), name='skribbl-reader')
                    snapshot = await ready
            except BaseException:
                await self._complete_close(flush=False, drain_timeout=0)
                raise
            return snapshot

    async def rejoin(self) -> Snapshot:
        """Close and log into the last lobby afresh, keeping handler registrations.

        Queued sends are cancelled. No server session or messages are replayed.
        Raise ValueError if this client has never received a lobby ID.
        """
        lobby = self.snapshot.lobby_id or self._last_lobby
        if not lobby:
            raise ValueError('No previous lobby is known')
        await self.close()
        return await self.connect(lobby)

    async def wait_closed(self) -> None:
        """Wait for cleanup and re-raise transport or handler failures.

        Wait from the owning task, not a handler awaiting its own dispatcher.
        Cancelling this waiter does not close the client; use the context manager.
        """
        if self._done is None:
            raise Disconnected('Client has not connected')
        # A cancelled caller must not cancel the shared completion future.
        error = await shield(self._done)
        if error is not None:
            raise error

    async def send_chat(self, text: TextSource) -> SendReceipt:
        """Wait for a paced socket write; the receipt is not a server acknowledgement."""
        return await self._sender().enqueue(text)

    async def send_guess(self, text: TextSource) -> SendReceipt:
        """Send within the current guessing turn; observe Event.guess_response for results."""
        return await self._sender().enqueue(text, guess=True)

    def enqueue_chat(self, text: TextSource) -> Future[SendReceipt]:
        """Queue chat and return a cancellable future; also publish queued_chat on completion.

        Text builders must be async callables and run after pacing. They must not
        await another send or close operation on the same client.
        """
        return self._sender().enqueue(text, queued=True)

    async def close(self, *, flush: bool = False, drain_timeout: float = 5.0) -> None:
        """Release resources, completing cleanup even if the caller is cancelled.

        Cancel queued sends by default. With flush=True, drain for at most
        drain_timeout seconds before cancelling the rest. Safe to call repeatedly.
        """
        if not isfinite(drain_timeout) or drain_timeout < 0:
            raise ValueError('Drain timeout must be finite and nonnegative')
        async with self._lifecycle:
            await self._complete_close(flush=flush, drain_timeout=drain_timeout)

    async def _complete_close(self, *, flush: bool, drain_timeout: float):
        cleanup = self._cleanup_task
        if cleanup is not None and current_task() in (self._reader, self._dispatcher):
            # Existing cleanup may be joining this task. Let its cancellation
            # finish the task instead of waiting on each other indefinitely.
            await shield(cleanup)
            return
        if cleanup is None:
            cleanup = create_task(
                self._close(flush=flush, drain_timeout=drain_timeout, caller=current_task()),
                name='skribbl-cleanup',
            )
            self._cleanup_task = cleanup
        cancelled = False
        while not cleanup.done():
            try:
                await shield(cleanup)
            except CancelledError:
                cancelled = True
        cleanup.result()
        if cancelled:
            raise CancelledError

    async def _close(self, *, flush: bool, drain_timeout: float, caller=None):
        if self._session is None:
            return
        if self._chat is not None:
            try:
                async with timeout(drain_timeout if flush else None):
                    await self._chat.close(flush=flush)
            except TimeoutError:
                await self._chat.close(flush=False)
        self._closing = True
        tasks: list[Task[None]] = []
        for task in (self._reader, self._dispatcher):
            if task is None:
                continue
            if current_task() is task or caller is task:
                continue
            task.cancel()
            tasks.append(task)
        await gather(*tasks, return_exceptions=True)
        if self._socket is not None:
            await self._socket.close()
        await self._session.close()
        self._session = None
        self._socket = None
        self._store.snapshot = replace(self.snapshot, ready=False)
        self._pending_guesses.clear()
        self._guess_outcomes.clear()
        while not self._event_queue.empty():
            self._event_queue.get_nowait()
        if self._done is not None and not self._done.done():
            self._done.set_result(self._error)

    def _sender(self):
        if not self.connected or self._chat is None:
            raise Disconnected('Client is not connected')
        return self._chat

    async def _send_chat(self, text: str, guess: bool, turn_id: int) -> SendReceipt:
        self._receipt_sequence += 1
        receipt = SendReceipt(self._receipt_sequence, text, turn_id, monotonic(), guess)
        if guess:
            self._pending_guesses.append(receipt)
        try:
            await self._send(PacketID.CHAT, text)
        except BaseException:
            if receipt in self._pending_guesses:
                self._pending_guesses.remove(receipt)
            raise
        return receipt

    async def _send(self, packet_id: int, data: Any = _MISSING) -> None:
        socket = self._socket
        if not self.connected or socket is None:
            raise Disconnected('Client is not connected')
        packet: dict[str, Any] = {'id': packet_id}
        if data is not _MISSING:
            packet['data'] = data
        await socket.emit('data', packet)

    async def _read(self):
        try:
            socket = self._socket
            if socket is None:
                raise Disconnected('Client is not connected')
            while True:
                name, data = await socket.read()
                if name == 'joinerr':
                    if type(data) is not int:
                        raise ProtocolError('Join rejection code must be an integer')
                    raise JoinRejected(data)
                if name == 'reason':
                    self.disconnect_reason = data
                elif name == 'data':
                    self._packet(data)
                else:
                    self._publish(name, data)
        except CancelledError:
            raise
        except Exception as error:
            self._fail(error)
        finally:
            if not self._closing and self._cleanup_task is None:
                self._closing = True
                self._cleanup_task = create_task(
                    self._close(flush=False, drain_timeout=0, caller=current_task()),
                    name='skribbl-cleanup',
                )

    async def _dispatch(self):
        try:
            while not self._closing:
                event = await self._event_queue.get()
                await self.dispatch(event)
                if current_task() is not self._dispatcher:
                    return
        except CancelledError:
            raise
        except Exception as error:
            self._fail(error)
            if self._reader is not None:
                self._reader.cancel()

    def _fail(self, error):
        if self._error is None:
            self._error = error
        if self._ready is not None and not self._ready.done():
            self._ready.set_exception(error)

    def _packet(self, packet):
        if not isinstance(packet, dict) or type(packet.get('id')) is not int:
            raise ProtocolError('Expected numeric packet ID')
        packet_id, data = packet['id'], packet.get('data')
        previous = self.snapshot
        state = self._store.apply(packet_id, data)
        if packet_id == PacketID.LOBBY:
            self._last_lobby = state.lobby_id
            ready = self._ready
            if ready is not None and not ready.done():
                ready.set_result(state)
        chat = self._chat
        if packet_id == PacketID.SPAM and chat is not None:
            chat.note_spam()
        if state.turn_id != previous.turn_id or state.phase not in (Phase.CHOOSING, Phase.DRAWING):
            self._pending_guesses.clear()
            self._guess_outcomes.clear()
        response = self._correlate(packet_id, data)
        ended = previous.phase in (Phase.CHOOSING, Phase.DRAWING) and state.phase not in (
            Phase.CHOOSING,
            Phase.DRAWING,
        )
        self._publish(
            _NAMES.get(packet_id, 'packet'),
            data,
            packet_id=packet_id,
            guess_response=response,
            previous_phase=previous.phase,
            turn_started=state.turn_id != previous.turn_id,
            turn_ended=ended,
        )

    def _correlate(self, packet_id: int, data: Any) -> Optional[GuessResponse]:
        outcome, text = None, None
        if packet_id == PacketID.CLOSE_GUESS:
            outcome, text = 'close', str(data)
        elif packet_id == PacketID.CHAT and data['id'] == self.snapshot.me:
            outcome, text = 'incorrect', data['msg']
        elif packet_id == PacketID.GUESSED and data['id'] == self.snapshot.me:
            outcome = 'correct'
        if outcome is not None:
            priority = {'incorrect': 0, 'close': 1, 'correct': 2}
            for index in range(len(self._pending_guesses) - 1, -1, -1):
                receipt = self._pending_guesses[index]
                if text is None or receipt.text.casefold() == text.casefold():
                    previous = self._guess_outcomes.get(receipt.id)
                    if previous is not None and priority[previous] >= priority[outcome]:
                        return None
                    self._guess_outcomes[receipt.id] = outcome
                    return GuessResponse(receipt, GuessOutcome(outcome))
        return None

    def _publish(self, name: str, data=None, **fields):
        self._sequence += 1
        if self.events is not None and name not in self.events:
            return
        event = Event(name, self.snapshot, self._sequence, monotonic(), freeze(data), **fields)
        if self._event_queue.full():
            error = QueueFull('Event buffer exhausted; increase it or shorten handlers')
            self._fail(error)
            reader = self._reader
            if reader is not None and current_task() is not reader:
                reader.cancel()
            raise error
        self._event_queue.put_nowait(event)
