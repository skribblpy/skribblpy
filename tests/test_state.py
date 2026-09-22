from copy import deepcopy
from pytest import mark, raises
from skribblpy.state import StateStore
from skribblpy import Phase, PacketID, DrawCommand, ProtocolError


def test_authoritative_snapshot_hints_and_immutable_players(lobby):
    store = StateStore()
    initial = store.apply(PacketID.LOBBY, lobby)
    assert initial.guessing_enabled and initial.hint == '___ ___'
    state = store.apply(PacketID.HINT, [[0, 'C'], [3, '-'], [5, 'a']])
    assert state.hint == 'C__-_a_'
    assert initial.hint == '___ ___'
    store.apply(PacketID.NAME, {'id': 2, 'name': 'Changed'})
    assert initial.players[2].name == 'Drawer'
    with raises(TypeError):
        # The published player mapping must reject mutation at runtime.
        # noinspection PyUnresolvedReferences
        initial.players[3] = initial.players[2]
    store.apply(PacketID.GUESSED, {'id': 1, 'word': 'Cat-dad'})
    assert not store.snapshot.guessing_enabled


def test_turn_lifecycle_scores_and_fresh_lobby(lobby):
    store = StateStore()
    state = store.apply(PacketID.LOBBY, lobby)
    turn = state.turn_id
    store.apply(
        PacketID.STATE,
        {
            'id': Phase.TURN_RESULT,
            'time': 5,
            'data': {'word': 'cat dog', 'scores': [1, 150, 100, 2, 30, 20]},
        },
    )
    assert store.snapshot.word == 'cat dog'
    assert store.snapshot.players[1].score == 150
    assert store.snapshot.turn_id == turn
    store.apply(PacketID.STATE, {'id': Phase.CHOOSING, 'data': {'words': ['cat', 'dog', 'sun']}})
    assert store.snapshot.is_drawer and not store.snapshot.drawing_enabled
    assert store.snapshot.turn_id == turn + 1
    store.apply(PacketID.STATE, {'id': Phase.DRAWING, 'data': {'id': 1, 'word': 'sun'}})
    assert store.snapshot.drawing_enabled and store.snapshot.turn_id == turn + 1
    store.reset()
    assert not store.snapshot.ready
    assert store.apply(PacketID.LOBBY, lobby).turn_id == turn + 2


def test_snapshot_preserves_already_guessed_players(lobby):
    lobby['users'][0]['guessed'] = True
    store = StateStore()
    assert not store.apply(PacketID.LOBBY, lobby).guessing_enabled


def test_drawing_history_undo_and_atomic_validation(lobby):
    store = StateStore(max_history=2)
    store.apply(PacketID.LOBBY, lobby)
    commands = [[0, 4, 4, 0, 0, 10, 10], [1, 1, 0, 0]]
    store.apply(PacketID.DRAW, commands)
    assert len(store.drawing) == 2
    with raises(ProtocolError):
        store.apply(PacketID.DRAW, commands)
    assert len(store.drawing) == 2
    store.apply(PacketID.UNDO, 1)
    assert store.drawing == [DrawCommand(tuple(commands[0]))]
    store.apply(PacketID.CLEAR, None)
    assert not store.drawing
    old = store.snapshot
    malformed = deepcopy(lobby)
    malformed['state']['data']['word'] = [-3]
    with raises(ProtocolError):
        store.apply(PacketID.LOBBY, malformed)
    assert store.snapshot is old


def test_disabled_history_still_reduces_state(lobby):
    store = StateStore(track_drawing=False)
    store.apply(PacketID.LOBBY, lobby)
    store.apply(PacketID.DRAW, [[1, 4, 0, 0]])
    assert not store.drawing and store.snapshot.ready


def test_vote_kick_counts_are_authoritative(lobby):
    store = StateStore()
    store.apply(PacketID.LOBBY, lobby)
    store.apply(PacketID.VOTE_KICK, [3, 1, 4, 5])
    state = store.apply(PacketID.VOTE_KICK, [2, 1, 5, 5])
    assert state.vote_kick is not None
    assert state.vote_kick.current_votes == 5
    assert state.vote_kick.observed_voters == {2, 3}
    store.apply(PacketID.VOTE_KICK, [2, 1, 1, 5])
    assert store.snapshot.vote_kick is not None
    assert store.snapshot.vote_kick.observed_voters == {2}
    store.apply(PacketID.PLAYER_LEAVE, {'id': 1, 'reason': 0})
    assert store.snapshot.vote_kick is None


@mark.parametrize('data', [[-1, 'a'], [1, ''], [[2]], [True, 'a']])
def test_reject_invalid_hints(lobby, data):
    store = StateStore()
    store.apply(PacketID.LOBBY, lobby)
    with raises(ProtocolError):
        store.apply(PacketID.HINT, data)


@mark.parametrize(
    'packet_id,data',
    [
        (PacketID.NAME, {'id': 1, 'name': ['mutable']}),
        (PacketID.AVATAR, {'id': 1, 'avatar': [[1], 2, 3, 4]}),
        (PacketID.STATE, {'id': Phase.CHOOSING, 'data': {'words': ['cat', {}]}}),
        (PacketID.STATE, {'id': Phase.CHOOSING, 'data': {'words': 'cat'}}),
        (PacketID.STATE, {'id': Phase.CHOOSING, 'data': {'id': True}}),
        (PacketID.STATE, {'id': Phase.DRAWING, 'data': {'id': True}}),
        (PacketID.STATE, {'id': Phase.TURN_RESULT, 'data': {'word': ['cat']}}),
    ],
)
def test_malformed_metadata_cannot_corrupt_an_immutable_snapshot(lobby, packet_id, data):
    store = StateStore()
    state = store.apply(PacketID.LOBBY, lobby)
    store.apply(PacketID.DRAW, [[1, 4, 0, 0]])
    with raises(ProtocolError):
        store.apply(packet_id, data)
    assert store.snapshot is state
    assert store.drawing == [DrawCommand.fill(4)]
