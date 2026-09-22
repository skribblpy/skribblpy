from json import dumps
from aiohttp import web
from asyncio import timeout
from pytest import mark, raises
from tests.test_client import LocalServer
from skribblpy import (
    Event,
    Client,
    PacketID,
    Snapshot,
    EventName,
    SettingID,
    SendReceipt,
    GuessOutcome,
    ProtocolError,
    MatchmakingError,
)


def test_typed_event_accessors_preserve_raw_data():
    state = Snapshot()
    chat = Event(EventName.CHAT, state, 1, 0, {'id': 4, 'msg': 'hello'}, PacketID.CHAT)
    message = chat.chat
    assert message is not None
    assert message.player_id == 4 and message.message == 'hello'
    assert chat.data['msg'] == 'hello' and chat.guessed is None
    guessed = Event(EventName.GUESSED, state, 2, 0, {'id': 4, 'word': 'cat'}, PacketID.GUESSED)
    update = guessed.guessed
    assert update is not None
    assert update.word == 'cat'
    hints = Event(EventName.HINT, state, 3, 0, ((1, 'a'), (3, '-')), PacketID.HINT)
    assert [(letter.index, letter.character) for letter in hints.hint_letters] == [
        (1, 'a'),
        (3, '-'),
    ]
    single = Event(EventName.HINT, state, 4, 0, (2, 'C'), PacketID.HINT)
    assert single.hint_letters[0].character == 'C'
    receipt = SendReceipt(1, 'cat', 1, 0)
    queued = Event(EventName.QUEUED_CHAT, state, 5, 0, receipt)
    result = queued.queued_chat
    assert result is not None
    assert result.receipt == receipt and result.error is None
    error = ValueError('builder failed')
    failed = Event(EventName.QUEUED_CHAT, state, 6, 0, error)
    failure = failed.queued_chat
    assert failure is not None
    assert failure.error is error
    assert GuessOutcome.CORRECT == 'correct'


async def test_connect_returns_snapshot_and_setting_enum_works(monkeypatch, lobby):
    async with LocalServer(monkeypatch, lobby) as server:
        async with Client(base_url=server.base_url) as client:
            snapshot = await client.connect()
            assert snapshot.ready and snapshot.lobby_id == 'ROOM'
            client._store.apply(PacketID.OWNER, 1)
            await client.set_setting(SettingID.DRAW_TIME, 80)


async def test_matchmaker_failure_has_stable_framework_error(monkeypatch, lobby):
    class Unavailable(LocalServer):
        async def matchmake(self, request):
            return web.Response(status=503)

    async with Unavailable(monkeypatch, lobby) as server:
        client = Client(base_url=server.base_url)
        with raises(MatchmakingError) as failure:
            await client.connect()
        assert failure.value.status == 503 and client._session is None


@mark.parametrize(
    'policy',
    [
        None,
        {},
        {'pingInterval': True, 'pingTimeout': 1000},
        {'pingInterval': 1000, 'pingTimeout': 1000, 'maxPayload': -1},
    ],
)
async def test_malformed_handshake_policy_is_a_protocol_error(monkeypatch, lobby, policy):
    class Malformed(LocalServer):
        async def websocket(self, request):
            socket = web.WebSocketResponse()
            await socket.prepare(request)
            self.sockets.append(socket)
            await socket.send_str('0' + dumps(policy))
            await socket.receive()
            return socket

    async with Malformed(monkeypatch, lobby) as server:
        client = Client(base_url=server.base_url)
        async with timeout(2):
            with raises(ProtocolError):
                await client.connect()
        assert client._session is None
