"""Small, bounded live check using only a newly created private lobby.

Run explicitly with ``python -m validation.live_private``. Results are printed as
JSON; no game-service calls are made by the ordinary pytest suite.
"""

from json import dumps
from typing import Any
from time import monotonic
from collections import Counter
from asyncio import run, sleep, timeout
from skribblpy import Phase, Client, DrawCommand

CHECK_TIMEOUT = 150
CUSTOM_WORDS = ('planet', 'guitar', 'castle', 'banana', 'rocket', 'window')


async def _wait(predicate, seconds=10):
    async with timeout(seconds):
        while not predicate():  # noqa: ASYNC110 - bounded observation of two independent clients
            await sleep(0.05)


async def check():
    started = monotonic()
    report: dict[str, Any] = {'checks': {}, 'events': {}, 'failures': []}
    clients = [Client(name='Python check A'), Client(name='Python check B')]
    observed = [[], []]
    for index, connection in enumerate(clients):

        @connection.on('*')
        async def observe(event, position=index, observed_client=connection):
            observed[position].append(event)
            if event.snapshot.phase == Phase.CHOOSING and event.snapshot.is_drawer:
                await observed_client.choose_word(0)

    host, guest = clients
    try:
        async with timeout(CHECK_TIMEOUT):
            await host.connect(create_private=True)
            report['checks']['private_creation'] = host.snapshot.lobby_type == 1
            await sleep(3)
            await guest.connect(host.snapshot.lobby_id)
            await _wait(lambda: len(host.snapshot.players) == 2)
            report['checks']['invite_join'] = guest.snapshot.lobby_id == host.snapshot.lobby_id
            for setting, value in ((1, 2), (2, 30), (3, 1), (5, 5), (7, 1)):
                await host.set_setting(setting, value)
                await sleep(0.2)
            settings = host.snapshot.settings
            assert settings is not None, 'Lobby settings are missing'
            report['settings'] = list(settings.raw)
            await host.start_game(CUSTOM_WORDS)
            await _wait(
                lambda: all(client.snapshot.phase == Phase.DRAWING for client in clients), 30
            )
            drawer = next(client for client in clients if client.snapshot.is_drawer)
            guesser = next(client for client in clients if client.snapshot.guessing_enabled)
            guesser_index = clients.index(guesser)
            word = drawer.snapshot.word
            assert word, 'Drawer did not receive the selected word'
            report['checks']['word_choice'] = bool(word)
            report['custom_word_selected'] = word in CUSTOM_WORDS
            report['chosen_word'] = word
            report['initial_hint'] = guesser.snapshot.hint
            await drawer.send_drawing(
                (DrawCommand.brush(4, 4, 10, 10, 30, 30), DrawCommand.brush(1, 4, 30, 30, 50, 10))
            )
            await _wait(lambda: len(guesser.drawing) == 2)
            report['checks']['drawing_playback'] = True
            await drawer.undo_drawing(1)
            await _wait(lambda: len(guesser.drawing) == 1)
            report['checks']['retained_count_undo'] = True
            await drawer.clear_drawing()
            await _wait(lambda: not guesser.drawing)
            report['checks']['clear'] = True
            await guesser.send_guess('protocol check incorrect')
            await _wait(
                lambda: any(
                    event.guess_response and event.guess_response.outcome == 'incorrect'
                    for event in observed[guesser_index]
                )
            )
            report['checks']['incorrect_guess'] = True
            await guesser.send_guess(word[:-1])
            await _wait(
                lambda: any(event.name == 'close_guess' for event in observed[guesser_index]), 5
            )
            report['checks']['close_guess'] = any(
                event.guess_response and event.guess_response.outcome == 'close'
                for event in observed[guesser_index]
            )
            await _wait(lambda: any(event.name == 'hint' for event in observed[guesser_index]), 28)
            report['checks']['hint_update'] = True
            report['revealed_hint'] = guesser.snapshot.hint
            await guesser.send_guess(word)
            await _wait(
                lambda: any(
                    event.guess_response and event.guess_response.outcome == 'correct'
                    for event in observed[guesser_index]
                )
            )
            report['checks']['correct_guess'] = True
            await _wait(lambda: guesser.snapshot.phase == Phase.TURN_RESULT)
            report['checks']['turn_reveal'] = guesser.snapshot.word == word
            before = guest.snapshot.me
            await sleep(2)
            await guest.rejoin()
            report['checks']['fresh_rejoin'] = guest.snapshot.me != before
            report['checks']['same_lobby'] = guest.snapshot.lobby_id == host.snapshot.lobby_id
    except Exception as error:
        report['failures'].append({'type': type(error).__name__, 'message': str(error)})
    finally:
        for connection in clients:
            await connection.close()
        report['checks']['closed'] = all(not client.connected for client in clients)
        report['events'] = {
            str(index): dict(Counter(event.name for event in events))
            for index, events in enumerate(observed)
        }
        report['guess_trace'] = [
            {
                'client': index,
                'name': event.name,
                'data': repr(event.data),
                'response': repr(event.guess_response),
            }
            for index, events in enumerate(observed)
            for event in events
            if event.name in ('close_guess', 'guessed', 'chat')
        ]
        report['seconds'] = monotonic() - started
        print(dumps(report, indent=2), flush=True)
    return not report['failures'] and all(report['checks'].values())


if __name__ == '__main__':
    raise SystemExit(0 if run(check()) else 1)
