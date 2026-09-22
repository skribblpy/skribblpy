from aiohttp import web
from pytest import raises
from time import monotonic
from typing import Optional
from json import dumps, loads
from skribblpy.transport import Shard
from collections.abc import Callable, Awaitable
from asyncio import Event, sleep, timeout, all_tasks, create_task, CancelledError
from skribblpy import Client, QueueFull, ActionError, JoinRejected, ProtocolError


class LocalServer:
    def __init__(self, monkeypatch, lobby, *, reject=0, early_ping=False, stall=False):
        self.lobby = lobby
        self.reject = reject
        self.early_ping = early_ping
        self.stall = stall
        self.messages = []
        self.logins = []
        self.forms = []
        self.sockets = []
        self.pong = Event()
        self.runner = None
        self.on_message: Optional[Callable[[web.WebSocketResponse, list], Awaitable[None]]] = None
        self.monkeypatch = monkeypatch

    async def __aenter__(self):
        app = web.Application()
        app.router.add_post('/api/play', self.matchmake)
        app.router.add_get('/socket', self.websocket)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        port = self.runner.addresses[0][1]
        self.base_url = f'http://127.0.0.1:{port}'
        self.monkeypatch.setattr(
            Shard,
            'parse',
            classmethod(
                lambda cls, value: Shard(self.base_url, '/socket', f'ws://127.0.0.1:{port}/socket')
            ),
        )
        return self

    async def __aexit__(self, *_):
        for socket in self.sockets:
            await socket.close()
        await self.runner.cleanup()

    async def matchmake(self, request):
        self.forms.append(dict(await request.post()))
        return web.Response(text='https://server.skribbl.io:5003')

    async def websocket(self, request):
        socket = web.WebSocketResponse()
        await socket.prepare(request)
        self.sockets.append(socket)
        if self.stall:
            await socket.receive()
            return socket
        await socket.send_str(
            '0{"sid":"local","pingInterval":1000,"pingTimeout":2000,"maxPayload":81920}'
        )
        assert await socket.receive_str() == '40'
        if self.early_ping:
            await socket.send_str('2')
            assert await socket.receive_str() == '3'
        await socket.send_str('40{"sid":"namespace"}')
        login = loads((await socket.receive_str())[2:])
        self.logins.append(login[1])
        if self.reject:
            await socket.send_str(f'42["joinerr",{self.reject}]')
        else:
            await socket.send_str('42' + dumps(['data', {'id': 10, 'data': self.lobby}]))
        async for message in socket:
            if message.data == '3':
                self.pong.set()
            elif isinstance(message.data, str) and message.data.startswith('42'):
                payload = loads(message.data[2:])
                self.messages.append((monotonic(), payload))
                if self.on_message is not None:
                    await self.on_message(socket, payload)
        return socket

    async def packet(self, packet_id, data):
        await self.sockets[-1].send_str('42' + dumps(['data', {'id': packet_id, 'data': data}]))


async def test_login_decorators_pacing_heartbeat_and_rejoin(monkeypatch, lobby):
    async with LocalServer(monkeypatch, lobby, early_ping=True) as server:
        client = Client(base_url=server.base_url)
        handled = Event()
        snapshots = []

        @client.on('lobby')
        async def on_lobby(event):
            snapshots.append(event.snapshot)
            await client.send_chat('first')
            await client.send_chat('second')
            handled.set()

        async with client:
            await client.connect()
            await server.sockets[-1].send_str('2')
            async with timeout(0.5):
                await server.pong.wait()
            async with timeout(3):
                await handled.wait()
            await sleep(0.02)
            assert server.logins[0]['join'] == ''
            assert server.logins[0]['name'] == ''
            assert server.forms == [{'lang': '0'}]
            assert server.messages[1][0] - server.messages[0][0] >= 0.98
            old_turn = client.snapshot.turn_id
            client.off('lobby', on_lobby)
            await client.rejoin()
            assert server.logins[-1]['join'] == 'ROOM'
            assert server.forms[-1] == {'id': 'ROOM'}
            assert client.snapshot.turn_id > old_turn
            assert snapshots[0].ready
        assert not client.connected and not client.snapshot.ready


async def test_cancel_queued_guess_and_revalidate_turn(monkeypatch, lobby):
    async with LocalServer(monkeypatch, lobby) as server:
        async with Client(base_url=server.base_url) as client:
            await client.connect()
            await client.send_chat('occupy pacing')
            cancelled = create_task(client.send_guess('cancel me'))
            await sleep(0.02)
            cancelled.cancel()
            with raises(CancelledError):
                await cancelled
            stale = create_task(client.send_guess('old turn'))
            await server.packet(11, {'id': 5, 'time': 5, 'data': {'word': 'cat dog'}})
            with raises(ActionError):
                await stale
            await sleep(0.02)
            assert [entry[1][1]['data'] for entry in server.messages] == ['occupy pacing']


async def test_spam_backoff_and_deferred_text(monkeypatch, lobby):
    async with LocalServer(monkeypatch, lobby) as server:
        async with Client(base_url=server.base_url, spam_backoff=1.2) as client:
            await client.connect()
            await client.send_chat('first')
            latest = ['before']

            async def current_text():
                return latest[0]

            queued = client.enqueue_chat(current_text)
            await server.packet(32, None)
            await sleep(0.1)
            latest[0] = 'after'
            await queued
            await sleep(0.02)
            assert server.messages[-1][1][1]['data'] == 'after'
            assert server.messages[-1][0] - server.messages[0][0] >= 1.18


async def test_guess_response_and_event_snapshot(monkeypatch, lobby):
    async with LocalServer(monkeypatch, lobby) as server:
        async with Client(base_url=server.base_url) as client:
            received = Event()
            responses = []

            @client.on('guessed')
            async def on_guessed(event):
                responses.append(event)
                received.set()

            await client.connect()
            receipt = await client.send_guess('cat dog')
            await server.packet(15, {'id': 1, 'word': 'cat dog'})
            async with timeout(1):
                await received.wait()
            assert responses[0].guess_response.receipt == receipt
            assert responses[0].guess_response.outcome == 'correct'
            assert not responses[0].snapshot.guessing_enabled
            with raises(TypeError):
                responses[0].data['word'] = 'mutate'


async def test_rejection_cleans_resources(monkeypatch, lobby):
    async with LocalServer(monkeypatch, lobby, reject=4) as server:
        client = Client(base_url=server.base_url)
        with raises(JoinRejected) as failure:
            await client.connect(create_private=True)
        assert failure.value.code == 4
        assert client._session is None
        assert server.logins[0]['join'] == 0 and server.logins[0]['create'] == 1


async def test_handler_failure_is_observable(monkeypatch, lobby):
    async with LocalServer(monkeypatch, lobby) as server:
        async with Client(base_url=server.base_url) as client:

            @client.on('hint')
            async def fail(_event):
                raise RuntimeError('handler failed')

            await client.connect()
            await server.packet(13, [0, 'a'])
            async with timeout(2):
                with raises(RuntimeError, match='handler failed'):
                    await client.wait_closed()


async def test_close_from_handler_and_bounded_flush(monkeypatch, lobby):
    async with LocalServer(monkeypatch, lobby) as server:
        client = Client(base_url=server.base_url)

        @client.on('hint')
        async def close(_event):
            await client.close(flush=True, drain_timeout=0.05)

        await client.connect()
        await client.send_chat('first')
        pending = client.enqueue_chat('never sent')
        await server.packet(13, [0, 'a'])
        async with timeout(1):
            await client.wait_closed()
        assert pending.cancelled()
        assert client._session is None


def test_shard_rewrite():
    shard = Shard.parse('https://server3.skribbl.io:5003')
    assert shard.websocket == 'wss://server3.skribbl.io/5003/?EIO=4&transport=websocket'
    with raises(ProtocolError):
        Shard.parse('https://server3.skribbl.io')


async def test_event_overflow_disconnects_with_observable_error(monkeypatch, lobby):
    async with LocalServer(monkeypatch, lobby) as server:
        async with Client(base_url=server.base_url, event_buffer=1) as client:
            entered = Event()

            @client.on('hint')
            async def slow(_event):
                entered.set()
                await Event().wait()

            await client.connect()
            await server.packet(13, [0, 'a'])
            await entered.wait()
            await server.packet(13, [1, 'b'])
            await server.packet(13, [2, 'c'])
            async with timeout(1):
                with raises(QueueFull, match='Event buffer'):
                    await client.wait_closed()


async def test_filtered_packets_still_reduce_state(monkeypatch, lobby):
    async with LocalServer(monkeypatch, lobby) as server:
        async with Client(base_url=server.base_url, events={'chat'}) as client:
            called = []

            @client.on('*')
            async def record(event):
                called.append(event)

            await client.connect()
            await server.packet(13, [0, 'a'])
            await sleep(0.03)
            assert client.snapshot.hint == 'a__ ___'
            assert not called


async def test_rejoin_from_handler_has_one_dispatcher(monkeypatch, lobby):
    async with LocalServer(monkeypatch, lobby) as server:
        async with Client(base_url=server.base_url) as client:
            rejoined = Event()

            @client.on('hint')
            async def rejoin(_event):
                await client.rejoin()
                rejoined.set()

            await client.connect()
            old_dispatcher = client._dispatcher
            assert old_dispatcher is not None
            await server.packet(13, [0, 'a'])
            async with timeout(2):
                await rejoined.wait()
            await sleep(0)
            assert old_dispatcher.done()
            dispatcher = client._dispatcher
            assert dispatcher is not None
            assert not dispatcher.done()


async def test_stalled_handshake_timeout_cleans_resources(monkeypatch, lobby):
    async with LocalServer(monkeypatch, lobby, stall=True) as server:
        client = Client(base_url=server.base_url, connect_timeout=0.1)
        async with timeout(2):
            with raises(TimeoutError):
                await client.connect()
        assert client._session is None
        assert not client.connected


async def test_graceful_flush_skips_cancelled_request(monkeypatch, lobby):
    async with LocalServer(monkeypatch, lobby) as server:
        client = Client(base_url=server.base_url)
        await client.connect()
        await client.send_chat('first')
        cancelled = client.enqueue_chat('skip')
        last = client.enqueue_chat('last')
        cancelled.cancel()
        await client.close(flush=True, drain_timeout=2)
        assert last.done() and not last.cancelled()
        assert [entry[1][1]['data'] for entry in server.messages] == ['first', 'last']


async def test_remote_disconnect_releases_every_owned_resource(monkeypatch, lobby):
    async with LocalServer(monkeypatch, lobby) as server:
        client = Client(base_url=server.base_url)
        await client.connect()
        http = client._session
        assert http is not None
        await server.sockets[-1].send_str('41')
        from skribblpy import Disconnected

        with raises(Disconnected):
            await client.wait_closed()
        assert http.closed and client._session is None
        dispatcher = client._dispatcher
        assert dispatcher is not None
        sender = client._chat
        assert sender is not None
        task = sender.task
        assert task is not None
        assert dispatcher.done() and task.done()
        assert not client.snapshot.ready
        await client.connect()
        await client.close()


async def test_repeated_close_cancellation_still_finishes_cleanup(monkeypatch, lobby):
    async with LocalServer(monkeypatch, lobby) as server:
        client = Client(base_url=server.base_url)
        await client.connect()
        await client.send_chat('first')
        pending = client.enqueue_chat('second')
        closing = create_task(client.close(flush=True, drain_timeout=0.15))
        await sleep(0.01)
        closing.cancel()
        await sleep(0.01)
        closing.cancel()
        with raises(CancelledError):
            await closing
        assert pending.cancelled()
        assert client._session is None
        assert not any(
            task.get_name().startswith('skribbl-') and not task.done() for task in all_tasks()
        )


async def test_close_response_upgrades_an_earlier_chat_echo(monkeypatch, lobby):
    async with LocalServer(monkeypatch, lobby) as server:
        async with Client(base_url=server.base_url) as client:
            responses = []

            @client.on('*')
            async def record(event):
                if event.guess_response is not None:
                    responses.append(event.guess_response)

            await client.connect()
            receipt = await client.send_guess('cat do')
            await server.packet(30, {'id': 1, 'msg': 'cat do'})
            await server.packet(16, 'cat do')
            await sleep(0.03)
            assert [(response.receipt.id, response.outcome) for response in responses] == [
                (receipt.id, 'incorrect'),
                (receipt.id, 'close'),
            ]


async def test_remote_disconnect_racing_handler_close_finishes_cleanup(monkeypatch, lobby):
    from skribblpy import Disconnected

    async with LocalServer(monkeypatch, lobby) as server:
        client = Client(base_url=server.base_url)
        handler_entered, cleanup_started, handler_closing = Event(), Event(), Event()

        @client.on('hint')
        async def close_from_handler(_event):
            handler_entered.set()
            await cleanup_started.wait()
            handler_closing.set()
            await client.close()

        await client.connect()
        sender = client._chat
        assert sender is not None
        original_close = sender.close

        async def close_chat(*, flush):
            cleanup_started.set()
            await handler_closing.wait()
            await original_close(flush=flush)

        sender.close = close_chat
        async with timeout(2):
            await server.packet(13, [0, 'a'])
            await handler_entered.wait()
            await server.sockets[-1].send_str('41')
            with raises(Disconnected):
                await client.wait_closed()
        assert client._session is None
        dispatcher = client._dispatcher
        assert dispatcher is not None
        assert dispatcher.cancelled()
        task = sender.task
        assert task is not None
        assert task.done()
