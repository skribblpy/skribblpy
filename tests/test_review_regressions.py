from json import dumps
from pytest import mark, raises
from skribblpy.images import ImageData
from skribblpy.events import EventHandlers
from examples.word_guesser.storage import WordStore
from asyncio import wait, sleep, gather, create_task, Event as Signal
from skribblpy import Event, Phase, Client, PacketID, Snapshot, EventName, ActionError, DrawCommand


async def test_send_image_does_not_resume_in_a_later_turn(lobby):
    client = Client()
    client._store.apply(PacketID.LOBBY, lobby)
    client._store.apply(PacketID.STATE, {'id': Phase.DRAWING, 'data': {'id': 1}})
    sent = []

    async def send(packet_id, _data=None):
        sent.append(packet_id)
        if packet_id == PacketID.CLEAR:
            client._store.apply(PacketID.STATE, {'id': Phase.TURN_RESULT, 'data': {}})
            client._store.apply(PacketID.STATE, {'id': Phase.DRAWING, 'data': {'id': 1}})

    client._send = send
    with raises(ActionError, match='turn changed'):
        await client.send_image(ImageData((DrawCommand.fill(4),), 1, 1, 1, 1))
    assert sent == [PacketID.CLEAR]


async def test_wildcard_event_dispatches_each_registration_once():
    handlers = EventHandlers()
    calls = []

    @handlers.on(EventName.ALL)
    async def record(received):
        calls.append(received)

    event = Event(EventName.ALL, Snapshot(), 1, 0)
    await handlers.dispatch(event)
    assert calls == [event]


def test_image_data_owns_an_immutable_command_sequence():
    commands = [DrawCommand.fill(4)]
    # Check defensive copying of a mutable input sequence.
    # noinspection PyTypeChecker
    image = ImageData(commands, 1, 1, 1, 1)
    commands.clear()
    assert image.commands == (DrawCommand.fill(4),)


@mark.parametrize(
    'data',
    [
        [],
        None,
        {'version': True, 'total_observations': 0, 'words': {}},
        {'version': 1, 'total_observations': False, 'words': {}},
        {'version': 1, 'total_observations': 1.0, 'words': {'cat': 1}},
    ],
)
async def test_malformed_statistics_rejected_without_writes(tmp_path, data):
    store = WordStore(tmp_path / 'words.json')
    original = dumps(data)
    store.statistics.write_text(original)
    with raises(ValueError):
        await store.observe('cat')
    assert store.statistics.read_text() == original


async def test_handler_can_be_cancelled_by_existing_cleanup():
    client = Client()
    release = Signal()

    async def finish_cleanup():
        await release.wait()

    cleanup = create_task(finish_cleanup())
    client._cleanup_task = cleanup
    dispatcher = create_task(client.close())
    client._dispatcher = dispatcher
    try:
        await sleep(0)
        dispatcher.cancel()
        done, _ = await wait((dispatcher,), timeout=0.2)
        assert dispatcher in done
        assert dispatcher.cancelled()
        assert not cleanup.done()
    finally:
        release.set()
        await gather(dispatcher, cleanup, return_exceptions=True)


@mark.parametrize('player_id', [True, 1.0, '1'])
async def test_player_actions_reject_noninteger_ids(lobby, player_id):
    client = Client()
    client._store.apply(PacketID.LOBBY, lobby)
    with raises(TypeError, match='Player ID'):
        # Noninteger IDs must fail at runtime.
        # noinspection PyTypeChecker
        await client.report(player_id, 1)


@mark.parametrize('words', ['apple', ['cat', 'dog', 1, 'sun', 'tree']])
async def test_custom_words_require_a_collection_of_strings(lobby, words):
    client = Client()
    client._store.apply(PacketID.LOBBY, lobby)
    client._store.apply(PacketID.OWNER, 1)
    with raises(TypeError, match='collection of strings'):
        # Malformed word collections must fail at runtime.
        # noinspection PyTypeChecker
        await client.start_game(words)


async def test_raw_draw_rejects_objects_that_only_look_like_commands(lobby):
    class InvalidCommand:
        values = (999,)

    client = Client()
    client._store.apply(PacketID.LOBBY, lobby)
    client._store.apply(PacketID.STATE, {'id': Phase.DRAWING, 'data': {'id': 1}})
    with raises(TypeError, match='DrawCommand'):
        # Duck-typed impostors must fail at runtime.
        # noinspection PyTypeChecker
        await client.draw(InvalidCommand())


@mark.parametrize('field', ['event_buffer', 'chat_queue_size', 'max_history'])
@mark.parametrize('value', [True, 1.5, float('nan'), float('inf')])
def test_client_capacity_limits_cannot_silently_become_unbounded(field, value):
    with raises(ValueError, match='positive integers'):
        Client(**{field: value})
