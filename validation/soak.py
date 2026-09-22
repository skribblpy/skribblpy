"""Timed loopback soak with concurrent guessers and copies of the real database."""

from json import dumps
from pathlib import Path
from time import monotonic
from shutil import copyfile
from typing import Annotated
from pytest import MonkeyPatch
from argly import App, Option, command
from tempfile import TemporaryDirectory
from tests.test_client import LocalServer
from skribblpy import Phase, Client, PacketID
from examples.word_guesser.ranking import WordIndex
from examples.word_guesser.storage import WordStore
from asyncio import run, sleep, gather, timeout, all_tasks, to_thread
from examples.word_guesser.session import GuesserSession, DEFAULT_DATABASE


async def soak(seconds: float, clients: int):
    if not 5 <= seconds <= 300 or not 1 <= clients <= 8:
        raise ValueError('Use 5..300 seconds and 1..8 loopback clients')
    source = Path(DEFAULT_DATABASE)
    lobby = {
        'id': 'LOCAL',
        'type': 1,
        'me': 1,
        'owner': 2,
        'round': 0,
        'settings': [0, 8, 80, 3, 3, 2, 0, 0],
        'users': [{'id': 1, 'name': 'Guesser'}, {'id': 2, 'name': 'Drawer'}],
        'state': {'id': Phase.PRIVATE_LOBBY, 'data': None},
    }
    with (
        TemporaryDirectory(prefix='skribbl-soak-') as temporary,
        MonkeyPatch.context() as monkeypatch,
    ):
        folder = Path(temporary)
        copyfile(source, folder / 'words.json')
        copyfile(source.with_name('words.stats.json'), folder / 'words.stats.json')
        store = WordStore(folder / 'words.json')
        words, counts = await store.load()
        sessions = []
        before = sum(counts.values())
        turns = 0
        started = monotonic()
        async with LocalServer(monkeypatch, lobby) as server:
            expected = {}

            async def respond(connection, payload):
                packet = payload[1]
                expected_word = expected.get(id(connection))
                if packet['id'] == PacketID.CHAT and packet['data'] == expected_word:
                    expected.pop(id(connection))
                    for packet_id, data in (
                        (15, {'id': 1, 'word': expected_word}),
                        (11, {'id': 5, 'data': {'word': expected_word}}),
                    ):
                        await connection.send_str(
                            '42' + dumps(['data', {'id': packet_id, 'data': data}])
                        )

            server.on_message = respond
            try:
                for _ in range(clients):
                    client = Client(base_url=server.base_url, track_drawing=False)
                    session = GuesserSession(client, store, WordIndex(words, counts))
                    sessions.append(session)
                    await client.connect()
                while monotonic() - started < seconds:
                    answer = ('cat', 'dog', 'sun', 'tree')[turns % 4]
                    turns += 1
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
                    async with timeout(10):
                        while expected or any(
                            turns not in session.learned_turns for session in sessions
                        ):
                            if any(session.error for session in sessions):
                                raise RuntimeError('A guesser failed during the soak')
                            await sleep(0.02)
            finally:
                await gather(*(session.close() for session in sessions))
        counts = await store.counts()
        leaked_tasks = [
            task.get_name()
            for task in all_tasks()
            if task.get_name().startswith(('skribbl-', 'guesser-')) and not task.done()
        ]
        lock_count = await to_thread(lambda: len(list(folder.glob('*.lock'))))
        report = {
            'seconds': monotonic() - started,
            'clients': clients,
            'turns_per_client': turns,
            'expected_observations_added': clients * turns,
            'observations_added': counts['total_observations'] - before,
            'leaked_tasks': leaked_tasks,
            'remaining_lock_files': lock_count,
        }
        assert report['observations_added'] == report['expected_observations_added']
        assert not leaked_tasks and not report['remaining_lock_files']
        print(dumps(report, indent=2))


@command('run', summary='Run concurrent guessers against a local server for a bounded duration.')
def main(
    *, seconds: Annotated[float, Option()] = 60.0, clients: Annotated[int, Option()] = 4
) -> int:
    run(soak(seconds, clients))
    return 0


if __name__ == '__main__':
    raise SystemExit(App.discover('skribbl-soak', 'validation.soak').run())
