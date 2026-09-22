from pytest import raises
from json import dumps, loads
from skribblpy import Event, Client, PacketID
from examples.word_guesser.ranking import WordIndex
from examples.word_guesser.storage import WordStore
from examples.word_guesser.session import GuesserSession
from asyncio import sleep, gather, create_task, CancelledError


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
    assert len(queued) == 2
    assert 'seen 1 times' in await queued[-1][0]()
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
