from json import dumps, loads
from pytest import mark, raises
from tests.test_client import LocalServer
from examples.word_guesser.ranking import WordIndex
from examples.word_guesser.storage import WordStore
from asyncio import sleep, gather, timeout, create_task, CancelledError, Event as Signal
from skribblpy import Event, Phase, Client, PacketID, EventName, SendReceipt, Disconnected
from examples.word_guesser.session import (
    Options,
    GREETING,
    run_pool,
    POST_GREETING,
    GuesserSession,
    EXHAUSTED_MESSAGE,
    POST_GUESS_SIGN_OFF_MESSAGES,
)


def test_case_hyphen_and_unknown_separator_matching():
    index = WordIndex(['Cat-dog', 'cat-dog', 'cat dog', 'cat-dig', 'sun', 'CAFÉ'])
    assert set(index.candidates('cat ___')) == {'cat-dog', 'cat dog', 'cat-dig'}
    assert set(index.candidates('cat-___')) == {'cat-dog', 'cat-dig'}
    assert index.candidates('C__-___') == ['Cat-dog']
    assert index.candidates('CAF_') == ['CAFÉ']
    assert index.matching_message('CAT-DOG', '___-___') == {'Cat-dog', 'cat-dog'}


def test_observations_and_close_guesses_reorder_without_discarding():
    index = WordIndex(['cat', 'dog', 'cot'], {'dog': 100, 'cat': 5})
    assert index.candidates('___')[0] == 'dog'
    result = index.candidates('___', close_guesses=['bat'])
    assert result[0] == 'cat' and set(result) == {'cat', 'dog', 'cot'}
    assert index.candidates('___', attempted={'dog', 'cot'}) == ['cat']


async def test_atomic_storage_compatible_records_and_shared_counts(tmp_path):
    path = tmp_path / 'words.json'
    path.write_text(dumps([{'word': 'cat'}]))
    first, second = WordStore(path), WordStore(path)
    await gather(*(store.observe('cat') for store in [first, second] * 10))
    assert (await first.counts())['total_observations'] == 20
    await gather(first.learn('ice-cream'), second.learn('café au lait'))
    records = loads(path.read_text(encoding='utf-8'))
    assert {record['word'] for record in records} == {'cat', 'ice-cream', 'café au lait'}
    cream = next(record for record in records if record['word'] == 'ice-cream')
    assert cream['contains_hyphen'] and cream['letter_count'] == 8 and cream['word_count'] == 1
    assert not list(tmp_path.glob('*.lock')) and not list(tmp_path.glob('*.tmp'))


async def test_cancel_wait_for_lock_does_not_remove_other_writer_lock(tmp_path):
    path = tmp_path / 'words.json'
    path.write_text('[]')
    lock = tmp_path / 'words.json.lock'
    lock.write_text('owned elsewhere')
    task = create_task(WordStore(path).learn('cat'))
    await sleep(0.02)
    task.cancel()
    with raises(CancelledError):
        await task
    assert lock.read_text() == 'owned elsewhere'


async def test_invalid_statistics_are_not_overwritten(tmp_path):
    path = tmp_path / 'words.json'
    path.write_text('[]')
    store = WordStore(path)
    original = '{"version":1,"total_observations":10,"words":{"cat":2}}'
    store.statistics.write_text(original)
    with raises(ValueError):
        await store.observe('cat')
    assert store.statistics.read_text() == original


async def test_session_replans_stops_and_records_once(lobby, tmp_path):
    path = tmp_path / 'words.json'
    path.write_text(dumps([{'word': 'cat dog'}, {'word': 'car dog'}]))
    client = Client()
    queued, started, cancelled = [], [], []

    def enqueue(text):
        from asyncio import get_running_loop

        future = get_running_loop().create_future()
        queued.append((text, future))
        return future

    async def send_guess(text):
        from asyncio import Event as Signal

        started.append(text)
        try:
            await Signal().wait()
        except CancelledError:
            cancelled.append(text)
            raise

    client.enqueue_chat = enqueue
    client.send_guess = send_guess
    store = WordStore(path)
    session = GuesserSession(client, store, WordIndex(['cat dog', 'car dog']))
    state = client._store.apply(PacketID.LOBBY, lobby)
    await session.handle(Event('lobby', state, 1, 0))
    await sleep(0)
    assert started
    state = client._store.apply(PacketID.HINT, [2, 't'])
    await session.handle(Event('hint', state, 2, 0))
    await sleep(0)
    assert cancelled and started[-1] == 'cat dog'
    state = client._store.apply(PacketID.GUESSED, {'id': 1, 'word': 'cat dog'})
    await session.handle(Event('guessed', state, 3, 0, {'id': 1, 'word': 'cat dog'}))
    assert session._guess_task is None
    state = client._store.apply(PacketID.STATE, {'id': 5, 'data': {'word': 'cat dog'}})
    await session.handle(Event('state', state, 4, 0))
    await session.handle(Event('state', state, 5, 0))
    assert (await store.counts())['words'] == {'cat dog': 1}
    assert len(queued) == 4
    assert [text for text, _ in queued[:2]] == [GREETING, POST_GREETING]
    assert 'seen 1 times' in await queued[-2][0]()
    assert queued[-1][0] in POST_GUESS_SIGN_OFF_MESSAGES
    await session.close()
    assert all(future.cancelled() for _, future in queued)


async def test_guesser_leaves_before_greeting_when_assigned_drawer(lobby, tmp_path):
    path = tmp_path / 'words.json'
    path.write_text('[]')
    lobby['state'] = {'id': 3, 'data': {'words': ['cat', 'dog', 'bird']}}
    client = Client()
    state = client._store.apply(PacketID.LOBBY, lobby)
    session = GuesserSession(client, WordStore(path), WordIndex([]))
    await session.handle(Event('lobby', state, 1, 0))
    assert session.depart.is_set()
    assert session._guess_task is None


@mark.parametrize('words', [['cat dog'], ['cat dog', 'dog cat']])
@mark.parametrize('ending', ['correct', 'turn_result', 'delayed_handler'])
async def test_no_guesses_or_exhaustion_after_turn_finishes(
    monkeypatch, lobby, tmp_path, words, ending
):
    database = tmp_path / 'words.json'
    database.write_text(dumps([{'word': word} for word in words]))
    async with LocalServer(monkeypatch, lobby) as server:
        client = Client(base_url=server.base_url)
        finished = Signal()
        release_handler = Signal()
        statistics_sent = Signal()

        @client.on(EventName.GUESSED)
        async def delay_handler(event):
            if ending == 'delayed_handler':
                finished.set()
                await release_handler.wait()

        session = GuesserSession(client, WordStore(database), WordIndex(words))

        @client.on(EventName.ALL)
        async def record_end(event):
            if event.name in (EventName.GUESSED, EventName.STATE):
                finished.set()

        async def respond(socket, payload):
            text = payload[1]['data']
            if text == 'cat dog':
                # Let the sender resume and queue its next guess or exhaustion message.
                await sleep(0.05)
                if ending == 'turn_result':
                    await server.packet(PacketID.STATE, {'id': 5, 'data': {'word': text}})
                else:
                    await server.packet(PacketID.GUESSED, {'id': 1, 'word': text})
            elif text.startswith('cat dog: seen'):
                statistics_sent.set()

        server.on_message = respond
        try:
            await client.connect()
            async with timeout(5):
                await finished.wait()
                # Cross the next pacing slot, including when event dispatch is blocked.
                await sleep(1.1)
                release_handler.set()
                if ending != 'turn_result':
                    await statistics_sent.wait()
            sent = [entry[1][1]['data'] for entry in server.messages]
            assert sent.count('cat dog') == 1
            assert 'dog cat' not in sent
            assert EXHAUSTED_MESSAGE not in sent
            assert session.error is None
        finally:
            release_handler.set()
            await session.close()


@mark.parametrize('reason', [None, 1, 2])
@mark.parametrize('failure', ['connect', 'wait_closed', 'clean_close'])
async def test_worker_reconnects_after_disconnect(monkeypatch, tmp_path, lobby, reason, failure):
    database = tmp_path / 'words.json'
    database.write_text('[]')
    connections, closed, delays = [], [], []
    lobby['state'] = {'id': Phase.WAITING}

    async def connect(client, lobby_id):
        connections.append((client, lobby_id))
        if len(connections) == 2:
            raise CancelledError
        client.disconnect_reason = reason
        if failure == 'connect':
            raise Disconnected('Connection lost during login')
        state = client._store.apply(PacketID.LOBBY, lobby)
        await client.dispatch(Event(EventName.LOBBY, state, 1, 0))
        return state

    async def wait_closed(client):
        if failure == 'wait_closed':
            raise Disconnected('Server disconnected')

    async def close(client):
        closed.append(client)

    async def pause(delay):
        delays.append(delay)

    def enqueue(client, text):
        from asyncio import get_running_loop

        future = get_running_loop().create_future()
        future.set_result(SendReceipt(1, text, 0, 0))
        return future

    monkeypatch.setattr(Client, 'connect', connect)
    monkeypatch.setattr(Client, 'wait_closed', wait_closed)
    monkeypatch.setattr(Client, 'close', close)
    monkeypatch.setattr(Client, 'enqueue_chat', enqueue)
    monkeypatch.setattr('examples.word_guesser.session.sleep', pause)
    await run_pool(Options(database=str(database), max_clients=1, lobby_id='ROOM'))
    assert len(connections) == 2
    assert connections[0][0] is not connections[1][0]
    assert [lobby_id for _, lobby_id in connections] == ['ROOM', 'ROOM']
    assert closed == [client for client, _ in connections]
    assert delays == [11 if failure == 'connect' else 10]


async def test_exhaustion_is_reported_once_per_turn(monkeypatch, lobby, tmp_path):
    client = Client()
    client._store.apply(PacketID.LOBBY, lobby)
    session = GuesserSession(client, WordStore(tmp_path / 'words.json'), WordIndex([]))
    sent = []

    async def send(text):
        sent.append(await text())

    monkeypatch.setattr(client, 'send_chat', send)
    try:
        await session.handle(Event(EventName.HINT, client.snapshot, 1, 0))
        await session._guess_task
        state = client._store.apply(PacketID.HINT, [0, 'c'])
        await session.handle(Event(EventName.HINT, state, 2, 0))
        await session._guess_task
        assert sent == [EXHAUSTED_MESSAGE]

        client._store.apply(PacketID.STATE, {'id': Phase.TURN_RESULT, 'data': {}})
        state = client._store.apply(PacketID.STATE, lobby['state'])
        await session.handle(Event(EventName.STATE, state, 3, 0))
        await session._guess_task
        assert sent == [EXHAUSTED_MESSAGE, EXHAUSTED_MESSAGE]
    finally:
        await session.close()
