from io import StringIO
from dataclasses import replace
from pytest import mark
from logging import INFO, LogRecord
from examples.word_guesser.ranking import WordIndex
from examples.word_guesser.storage import WordStore
from asyncio import sleep, CancelledError, get_running_loop
from examples.word_guesser.session import Options, run_pool, GuesserSession
from skribblpy import Event, Phase, Client, Player, Snapshot, EventName, SendReceipt
from examples.word_guesser.logging import (
    EventLogger,
    SessionLogger,
    ConsoleFormatter,
    configure_logging,
)


def test_session_labels_and_color_preserve_plain_records(caplog):
    caplog.set_level(INFO)
    logger = SessionLogger(2, 3)
    logger.info('Correct! The answer was %r.', 'cat', extra={'color': 'success'})
    record = caplog.records[-1]
    assert record.getMessage() == "[Bot 02] [Session 3] Correct! The answer was 'cat'."
    assert '\x1b[' not in ConsoleFormatter().format(record)
    colored = ConsoleFormatter(color=True).format(record)
    assert colored.startswith('\x1b[32m') and colored.endswith('\x1b[0m')
    assert '\x1b[' not in record.getMessage()


@mark.parametrize(
    'tty,no_color,expected', [(True, False, True), (False, False, False), (True, True, False)]
)
def test_console_color_respects_redirection_and_environment(monkeypatch, tty, no_color, expected):
    class Console(StringIO):
        def isatty(self):
            return tty

    stream = Console()
    monkeypatch.setattr('sys.stderr', stream)
    monkeypatch.delenv('NO_COLOR', raising=False)
    monkeypatch.delenv('TERM', raising=False)
    if no_color:
        monkeypatch.setenv('NO_COLOR', '')
    handlers = []
    monkeypatch.setattr(
        'examples.word_guesser.logging.basicConfig',
        lambda **kwargs: handlers.extend(kwargs['handlers']),
    )
    configure_logging()
    record = LogRecord('guesser', INFO, '', 1, 'test', (), None)
    record.color = 'join'
    assert ('\x1b[' in handlers[0].format(record)) is expected


def test_event_logging_keeps_departed_names_and_reports_game_transitions(caplog):
    caplog.set_level(INFO)
    logger = EventLogger(SessionLogger(1, 4))
    state = Snapshot(
        ready=True,
        me=1,
        round=2,
        turn_id=8,
        phase=Phase.DRAWING,
        players={1: Player(1, 'Bot'), 2: Player(2, 'Guest')},
    )
    logger.log(Event(EventName.LOBBY, state, 1, 0))
    logger.log(Event(EventName.PLAYER_JOIN, state, 2, 0, {'id': 2}))
    logger.log(Event(EventName.CHAT, state, 3, 0, {'id': 2, 'msg': 'hello\nworld'}))
    logger.log(Event(EventName.GUESSED, state, 4, 0, {'id': 2}))
    left = replace(state, players={1: state.players[1]})
    logger.log(Event(EventName.PLAYER_LEAVE, left, 5, 0, {'id': 2}))
    result = replace(left, phase=Phase.TURN_RESULT, word='cat')
    logger.log(Event(EventName.STATE, result, 6, 0, {'id': 5}, turn_ended=True))
    match = replace(result, phase=Phase.GAME_RESULT)
    # A queued send may capture newer state before the corresponding packet is dispatched.
    logger.log(Event(EventName.QUEUED_CHAT, match, 7, 0))
    ended = Event(EventName.STATE, match, 8, 0, {'id': 6, 'data': [[1, 0, 100]]})
    logger.log(ended)
    logger.log(ended)
    logger.log(Event(EventName.STATE, replace(match, phase=Phase.ROUND, round=1), 9, 0, {'id': 2}))
    messages = [record.getMessage() for record in caplog.records]
    assert all(message.startswith('[Bot 01] [Session 4]') for message in messages)
    assert "'Guest' (2) joined the lobby." in caplog.text
    assert "'Guest' (2) left the lobby." in caplog.text
    assert "Chat | 'Guest' (2): 'hello\\nworld'" in caplog.text
    assert "'Guest' (2) guessed correctly." in caplog.text
    assert "Turn 8 ended (round 2). Answer: 'cat'." in caplog.text
    assert sum('Round 2 finished.' in message for message in messages) == 1
    assert sum('Match finished in place 1.' in message for message in messages) == 1
    assert 'New game started at round 1.' in caplog.text


async def test_message_logs_wait_for_receipts_and_ignore_cancellation(
    monkeypatch, tmp_path, caplog
):
    caplog.set_level(INFO)
    client = Client()
    futures = []

    def enqueue(_):
        future = get_running_loop().create_future()
        futures.append(future)
        return future

    monkeypatch.setattr(client, 'enqueue_chat', enqueue)
    session = GuesserSession(
        client, WordStore(tmp_path / 'words.json'), WordIndex([]), SessionLogger(1, 1)
    )
    session._queue_message('hello', 'greeting', 'Sent greeting to the lobby.')
    assert 'Sent greeting' not in caplog.text
    futures[-1].set_result(SendReceipt(1, 'hello', 1, 0))
    await sleep(0)
    assert 'Sent greeting to the lobby.' in caplog.text
    session._queue_message('stats', 'post-guess statistics', 'Sent statistics.')
    futures[-1].set_exception(OSError('socket closed'))
    await sleep(0)
    assert 'Could not send post-guess statistics: socket closed' in caplog.text
    assert 'Sent statistics.' not in caplog.text
    session._queue_message('cancelled', 'cancelled message', 'Sent cancelled message.')
    await session.close()
    await sleep(0)
    assert 'cancelled message' not in caplog.text


@mark.parametrize('quiet', [False, True])
async def test_sent_guess_logging_and_quiet_hints(monkeypatch, tmp_path, caplog, quiet):
    caplog.set_level(INFO)
    client = Client()
    state = Snapshot(
        ready=True,
        me=1,
        drawer_id=2,
        turn_id=3,
        phase=Phase.DRAWING,
        hint='c_t',
        players={1: Player(1)},
    )
    client._store.snapshot = state
    session = GuesserSession(
        client,
        WordStore(tmp_path / 'words.json'),
        WordIndex(['cat']),
        SessionLogger(1, 2),
        quiet_hints=quiet,
    )

    async def send(_):
        client._store.snapshot = replace(state, phase=Phase.TURN_RESULT)
        return SendReceipt(1, 'cat', 3, 0, True)

    monkeypatch.setattr(client, 'send_guess', send)
    await session._guess(3)
    assert "Guessed 'cat' for hint 'c_t' (turn 3)." in caplog.text
    assert ('1 candidates remain.' in caplog.text) is not quiet
    await session.close()


async def test_worker_logs_session_numbers_and_retry_reason(monkeypatch, tmp_path, caplog):
    caplog.set_level(INFO)
    database = tmp_path / 'words.json'
    database.write_text('[{"word": "cat"}]')
    connections = 0
    delays = []

    async def connect(*_):
        nonlocal connections
        connections += 1
        if connections == 2:
            raise CancelledError
        raise OSError('offline')

    async def pause(delay):
        delays.append(delay)

    monkeypatch.setattr(Client, 'connect', connect)
    monkeypatch.setattr('examples.word_guesser.session.sleep', pause)
    await run_pool(Options(database=str(database), max_clients=1))
    assert delays == [11]
    assert '[Bot 01] [Session 1] Connection failed: offline' in caplog.text
    assert (
        '[Bot 01] [Session 1] Connection ended. Joining another public lobby in 11 seconds.'
        in caplog.text
    )
    assert '[Bot 01] [Session 2] Connecting to a public lobby.' in caplog.text
