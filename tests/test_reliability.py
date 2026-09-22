from pathlib import Path
from copy import deepcopy
from pytest import raises
from sys import executable
from shutil import copyfile
from typing import Optional
from json import dumps, loads
from tests.test_client import LocalServer
from threading import Event as ThreadEvent
from examples.word_guesser.ranking import WordIndex
from examples.word_guesser.storage import WordStore
from skribblpy import Phase, Client, PacketID, Snapshot
from examples.word_guesser.session import GuesserSession, DEFAULT_DATABASE
from asyncio import (
    Event,
    sleep,
    gather,
    timeout,
    all_tasks,
    to_thread,
    subprocess,
    create_task,
    CancelledError,
    create_subprocess_exec,
)


async def test_repeated_transaction_cancellation_does_not_release_lock_early(tmp_path):
    started, release = ThreadEvent(), ThreadEvent()
    store = WordStore(tmp_path / 'words.json')
    store.database.write_text('[]')
    original = store._observe

    def slow(word):
        started.set()
        release.wait(3)
        return original(word)

    store._observe = slow
    task = create_task(store.observe('cat'))
    await to_thread(started.wait, 1)
    task.cancel()
    await sleep(0)
    task.cancel()
    await sleep(0.02)
    assert await to_thread(Path(str(store.statistics) + '.lock').exists)
    assert not task.done()
    release.set()
    with raises(CancelledError):
        await task
    assert (await store.counts())['words'] == {'cat': 1}
    assert not await to_thread(Path(str(store.statistics) + '.lock').exists)


async def test_concurrent_guessers_update_copied_real_database(monkeypatch, lobby, tmp_path):
    source = Path(DEFAULT_DATABASE).parent
    database = tmp_path / 'words.json'
    statistics = tmp_path / 'words.stats.json'
    copyfile(source / 'words.json', database)
    copyfile(source / 'words.stats.json', statistics)
    store = WordStore(database)
    words, counts = await store.load()
    before = sum(counts.values())
    lobby = deepcopy(lobby)
    lobby['state'] = {'id': Phase.PRIVATE_LOBBY, 'data': None}
    answers = ('cat', 'dog', 'sun')
    sessions = []
    async with LocalServer(monkeypatch, lobby) as server:
        expected = {}

        async def respond(connection, payload):
            packet = payload[1]
            expected_word = expected.get(id(connection))
            if packet['id'] == PacketID.CHAT and expected_word == packet['data']:
                expected.pop(id(connection))
                await connection.send_str(
                    '42' + dumps(['data', {'id': 15, 'data': {'id': 1, 'word': expected_word}}])
                )
                await connection.send_str(
                    '42'
                    + dumps(
                        ['data', {'id': 11, 'data': {'id': 5, 'data': {'word': expected_word}}}]
                    )
                )

        server.on_message = respond
        try:
            for _ in range(4):
                client = Client(base_url=server.base_url, track_drawing=False)
                session = GuesserSession(client, store, WordIndex(words, counts))
                sessions.append(session)
                await client.connect()
            for turn, answer in enumerate(answers, 1):
                for socket in server.sockets:
                    expected[id(socket)] = answer
                    game = {
                        'id': 4,
                        'time': 80,
                        'data': {
                            'id': 2,
                            'word': [len(answer)],
                            'hints': [[index, char] for index, char in enumerate(answer)],
                        },
                    }
                    await socket.send_str('42' + dumps(['data', {'id': 11, 'data': game}]))
                async with timeout(8):

                    def pending(current_turn=turn):
                        return expected or any(
                            current_turn not in item.learned_turns for item in sessions
                        )

                    while pending():  # noqa: ASYNC110 - observe independent worker persistence
                        await sleep(0.02)
                assert not any(session.error for session in sessions)
            final = await store.counts()
            assert final['total_observations'] == before + len(sessions) * len(answers)
            for answer in answers:
                assert final['words'][answer] == counts.get(answer, 0) + len(sessions)
        finally:
            await gather(*(session.close() for session in sessions))
    assert not list(tmp_path.glob('*.lock'))
    assert not any(
        task.get_name().startswith(('skribbl-', 'guesser-')) and not task.done()
        for task in all_tasks()
    )


async def test_reconnect_soak_has_bounded_tasks_and_handlers(monkeypatch, lobby):
    async with LocalServer(monkeypatch, lobby) as server:
        client = Client(base_url=server.base_url, track_drawing=False)
        events = []
        received = Event()

        @client.on('hint')
        async def receive(event):
            events.append(event)
            received.set()

        prior: Optional[Snapshot] = None
        for _ in range(30):
            received.clear()
            await client.connect()
            await server.packet(13, [0, 'C'])
            async with timeout(1):
                await received.wait()
            assert client.snapshot.hint == 'C__ ___'
            if prior:
                assert prior.hint == 'C__ ___' and prior.ready
            prior = client.snapshot
            await client.close()
        assert len(events) == 30
        assert len(client._handlers['hint']) == 1
        assert not any(
            task.get_name().startswith('skribbl-') and not task.done() for task in all_tasks()
        )


async def test_separate_processes_preserve_shared_word_counts(tmp_path):
    database = tmp_path / 'words.json'
    database.write_text('[]')
    script = """
from asyncio import run
from sys import argv
from examples.word_guesser.storage import WordStore
async def main():
    store = WordStore(argv[1])
    for number in range(10):
        await store.observe('cat')
        await store.learn('word ' + str(number))
run(main())
"""
    processes = [
        await create_subprocess_exec(
            executable, '-c', script, str(database), stdout=subprocess.PIPE, stderr=subprocess.PIPE
        )
        for _ in range(3)
    ]
    async with timeout(20):
        outputs = await gather(*(process.communicate() for process in processes))
    for process, (_, stderr) in zip(processes, outputs, strict=True):
        assert process.returncode == 0, stderr.decode()
    counts = await WordStore(database).counts()
    assert counts['words'] == {'cat': 30}
    assert len(loads(database.read_text())) == 10
    assert not list(tmp_path.glob('*.lock'))


async def test_failed_replace_preserves_file_and_releases_lock(monkeypatch, tmp_path):
    store = WordStore(tmp_path / 'words.json')
    await store.observe('cat')
    before = store.statistics.read_bytes()

    def fail_replace(*_):
        raise OSError('simulated disk failure')

    monkeypatch.setattr('examples.word_guesser.storage.replace', fail_replace)
    with raises(OSError, match='simulated disk failure'):
        await store.observe('dog')
    assert store.statistics.read_bytes() == before
    assert not list(tmp_path.glob('*.lock')) and not list(tmp_path.glob('*.tmp'))
