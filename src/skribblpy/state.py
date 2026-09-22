"""Authoritative reducer with copy-on-write player maps and separate drawing history."""

from dataclasses import replace
from typing import Any, Optional
from types import MappingProxyType
from skribblpy.drawing import DrawCommand
from skribblpy.errors import ProtocolError
from skribblpy.constants import MAX_HINT_LENGTH, MAX_DRAWING_HISTORY
from skribblpy.models import Phase, Player, PacketID, Settings, Snapshot, VoteKick


def _integer(value: Any) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolError('Expected an integer')
    return value


def _player(data: dict) -> Player:
    if not isinstance(data, dict) or not isinstance(data.get('name', ''), str):
        raise ProtocolError('Expected a player object with a string name')
    return Player(
        _integer(data['id']),
        data.get('name', ''),
        tuple(_integer(value) for value in data.get('avatar', ())),
        _integer(data.get('score', 0)),
        bool(data.get('guessed', False)),
        _integer(data.get('flags', 0)),
    )


def _hints(data: list) -> dict[int, str]:
    if not isinstance(data, list):
        raise ProtocolError('Expected hint pairs')
    pairs = [data] if len(data) == 2 and type(data[0]) is int else data
    result = {}
    for pair in pairs:
        if not isinstance(pair, list) or len(pair) != 2:
            raise ProtocolError('Invalid hint pair')
        index, character = pair
        if _integer(index) < 0 or not isinstance(character, str) or len(character) != 1:
            raise ProtocolError('Invalid hint index or character')
        result[index] = character
    return result


class StateStore:
    """Apply packets synchronously on the client's event loop."""

    def __init__(self, *, track_drawing: bool = True, max_history: int = MAX_DRAWING_HISTORY):
        self.snapshot = Snapshot()
        self.track_drawing = track_drawing
        self.max_history = max_history
        self.drawing: list[DrawCommand] = []
        self._next_turn = 0

    def reset(self):
        self.snapshot = Snapshot()
        self.drawing.clear()

    def apply(self, packet_id: int, data: Any) -> Snapshot:
        """Malformed packets leave the published snapshot unchanged."""
        try:
            return self._apply(packet_id, data)
        except (KeyError, TypeError, IndexError, ValueError) as error:
            raise ProtocolError(f'Invalid packet {packet_id}: {error}') from error

    def _apply(self, packet_id: int, data: Any) -> Snapshot:
        state = self.snapshot
        drawing = None
        if packet_id == PacketID.LOBBY:
            players = {_integer(item['id']): _player(item) for item in data['users']}
            raw = tuple(_integer(item) for item in data['settings'])
            state = Snapshot(
                ready=True,
                lobby_id=data['id'],
                lobby_type=data['type'],
                me=_integer(data['me']),
                owner=_integer(data['owner']),
                round=_integer(data['round']),
                players=MappingProxyType(players),
                settings=Settings(raw) if len(raw) >= 8 else None,
            )
            state, drawing = self._phase(state, data['state'], initial=True)
            if drawing is None:
                drawing = []
        elif packet_id == PacketID.STATE:
            state, drawing = self._phase(state, data)
        elif packet_id == PacketID.PLAYER_JOIN:
            player = _player(data)
            state = replace(state, players=MappingProxyType({**state.players, player.id: player}))
        elif packet_id == PacketID.PLAYER_LEAVE:
            player_id = _integer(data['id'])
            players = dict(state.players)
            players.pop(player_id, None)
            vote = state.vote_kick
            state = replace(
                state,
                players=MappingProxyType(players),
                vote_kick=None if vote and vote.target_id == player_id else vote,
            )
        elif packet_id in (PacketID.AVATAR, PacketID.GUESSED, PacketID.NAME):
            player_id = _integer(data['id'])
            if (
                packet_id == PacketID.GUESSED
                and data.get('word') is not None
                and not isinstance(data['word'], str)
            ):
                raise ProtocolError('Guessed word must be a string')
            if player_id in state.players:
                current = state.players[player_id]
                if packet_id == PacketID.AVATAR:
                    avatar = tuple(_integer(value) for value in data.get('avatar', ()))
                    player = replace(current, avatar=avatar)
                elif packet_id == PacketID.NAME:
                    name = data.get('name', '')
                    if not isinstance(name, str):
                        raise ProtocolError('Player name must be a string')
                    player = replace(current, name=name)
                else:
                    player = replace(current, guessed=True)
                state = replace(
                    state, players=MappingProxyType({**state.players, player_id: player})
                )
        elif packet_id == PacketID.SETTING and state.settings:
            index, value = _integer(data['id']), _integer(data['val'])
            if 0 <= index < len(state.settings.raw):
                settings = list(state.settings.raw)
                settings[index] = value
                state = replace(state, settings=Settings(tuple(settings)))
        elif packet_id == PacketID.HINT:
            revealed = _hints(data)
            if state.hint is not None:
                hint = list(state.hint)
                for index, character in revealed.items():
                    if index < len(hint):
                        hint[index] = character
                state = replace(state, hint=''.join(hint))
        elif packet_id == PacketID.TIME:
            state = replace(state, remaining_time=_integer(data))
        elif packet_id == PacketID.CHAT:
            _integer(data['id'])
            if not isinstance(data['msg'], str):
                raise ProtocolError('Chat message must be a string')
        elif packet_id == PacketID.CLOSE_GUESS:
            if not isinstance(data, str):
                raise ProtocolError('Close guess must be a string')
        elif packet_id == PacketID.OWNER:
            state = replace(state, owner=_integer(data))
        elif packet_id == PacketID.VOTE_KICK:
            if len(data) != 4:
                raise ProtocolError('Vote kick requires four values')
            voter, target, count, required = map(_integer, data)
            previous = state.vote_kick
            voters: frozenset[int] = frozenset()
            if previous and previous.target_id == target and count >= previous.current_votes:
                voters = previous.observed_voters
            vote = VoteKick(target, count, required, voter, voters | {voter}) if count else None
            state = replace(state, vote_kick=vote)
        elif packet_id == PacketID.DRAW and self.track_drawing:
            commands = self._commands(data, len(self.drawing))
            self.drawing.extend(commands)
        elif packet_id == PacketID.CLEAR:
            self.drawing.clear()
        elif packet_id == PacketID.UNDO and self.track_drawing:
            retained = max(0, _integer(data))
            del self.drawing[retained:]

        if drawing is not None:
            self.drawing = drawing
        self._next_turn = max(self._next_turn, state.turn_id)
        self.snapshot = state
        return state

    def _commands(self, data: list, existing: int = 0) -> list[DrawCommand]:
        if not isinstance(data, list) or len(data) + existing > self.max_history:
            raise ProtocolError('Drawing history limit exceeded or invalid commands')
        return [DrawCommand(tuple(command)) for command in data]

    def _phase(self, state: Snapshot, game: dict, *, initial: bool = False):
        phase = _integer(game['id'])
        data = game.get('data')
        turn = state.turn_id
        new_turn = (
            (phase == Phase.CHOOSING and state.phase != phase)
            or (phase == Phase.DRAWING and state.phase not in (Phase.CHOOSING, Phase.DRAWING))
            or (initial and phase in (Phase.CHOOSING, Phase.DRAWING, Phase.TURN_RESULT))
        )
        if new_turn:
            turn = self._next_turn + 1
        state = replace(
            state,
            phase=phase,
            remaining_time=_integer(game.get('time', 0)),
            drawer_id=None,
            word=None,
            hint=None,
            word_choices=(),
            turn_id=turn,
        )
        drawing: Optional[list[DrawCommand]] = [] if new_turn else None
        if phase == Phase.ROUND:
            state = replace(state, round=_integer(data))
        elif phase == Phase.CHOOSING:
            if not isinstance(data, dict):
                raise ProtocolError('Turn metadata must be an object')
            words = data.get('words')
            if words is None:
                words = ()
            if not isinstance(words, (list, tuple)) or any(
                not isinstance(word, str) for word in words
            ):
                raise ProtocolError('Word choices must be an array of strings')
            drawer = data.get('id', state.me if words else None)
            if drawer is not None:
                drawer = _integer(drawer)
            state = replace(state, drawer_id=drawer, word_choices=tuple(words))
        elif phase == Phase.DRAWING:
            if not isinstance(data, dict):
                raise ProtocolError('Turn metadata must be an object')
            word = data.get('word')
            hint = None
            if isinstance(word, list):
                if (
                    any(_integer(size) < 0 for size in word)
                    or sum(word) + len(word) > MAX_HINT_LENGTH
                ):
                    raise ProtocolError('Invalid word lengths')
                cells = list(' '.join('_' * size for size in word))
                for index, character in _hints(data.get('hints', [])).items():
                    if index < len(cells):
                        cells[index] = character
                hint = ''.join(cells)
            elif word is not None and not isinstance(word, str):
                raise ProtocolError('Invalid drawing word')
            players = state.players
            if not initial:
                players = MappingProxyType(
                    {key: replace(player, guessed=False) for key, player in players.items()}
                )
            state = replace(
                state,
                drawer_id=_integer(data['id']) if data.get('id') is not None else None,
                hint=hint,
                players=players,
                word=word if isinstance(word, str) else None,
            )
            if self.track_drawing and data.get('drawCommands') is not None:
                drawing = self._commands(data['drawCommands'])
        elif phase == Phase.TURN_RESULT:
            if not isinstance(data, dict):
                raise ProtocolError('Turn metadata must be an object')
            word = data.get('word')
            if word is not None and not isinstance(word, str):
                raise ProtocolError('Revealed word must be a string')
            state = replace(state, word=word)
            scores = data.get('scores', [])
            if len(scores) % 3:
                raise ProtocolError('Invalid score triples')
            players = dict(state.players)
            for index in range(0, len(scores), 3):
                player_id, score, _ = scores[index : index + 3]
                if player_id in players:
                    players[player_id] = replace(players[player_id], score=_integer(score))
            state = replace(state, players=MappingProxyType(players))
        elif phase == Phase.PRIVATE_LOBBY:
            state = replace(
                state,
                players=MappingProxyType(
                    {
                        key: replace(player, score=0, guessed=False)
                        for key, player in state.players.items()
                    }
                ),
            )
        return state, drawing
