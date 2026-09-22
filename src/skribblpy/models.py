"""Immutable, inexpensive views of protocol state."""

from random import randrange
from typing import Any, Optional
from enum import IntEnum, StrEnum
from types import MappingProxyType
from collections.abc import Mapping
from dataclasses import field, dataclass
from skribblpy.constants import (
    AVATAR_EYE_VARIANTS,
    AVATAR_COLOR_VARIANTS,
    AVATAR_MOUTH_VARIANTS,
    ORDINARY_AVATAR_SPECIAL,
)


class PacketID(IntEnum):
    PLAYER_JOIN = 1
    PLAYER_LEAVE = 2
    KICK = 3
    BAN = 4
    VOTE_KICK = 5
    REPORT = 6
    MUTE = 7
    VOTE = 8
    AVATAR = 9
    LOBBY = 10
    STATE = 11
    SETTING = 12
    HINT = 13
    TIME = 14
    GUESSED = 15
    CLOSE_GUESS = 16
    OWNER = 17
    CHOOSE_WORD = 18
    DRAW = 19
    CLEAR = 20
    UNDO = 21
    START = 22
    END = 23
    CHAT = 30
    START_ERROR = 31
    SPAM = 32
    NAME = 90


class Phase(IntEnum):
    WAITING = 0
    STARTING = 1
    ROUND = 2
    CHOOSING = 3
    DRAWING = 4
    TURN_RESULT = 5
    GAME_RESULT = 6
    PRIVATE_LOBBY = 7


class EventName(StrEnum):
    ALL = '*'
    LOBBY = 'lobby'
    STATE = 'state'
    PLAYER_JOIN = 'player_join'
    PLAYER_LEAVE = 'player_leave'
    HINT = 'hint'
    TIME = 'time'
    CHAT = 'chat'
    GUESSED = 'guessed'
    CLOSE_GUESS = 'close_guess'
    SPAM = 'spam'
    VOTE_KICK = 'vote_kick'
    DRAWING_VOTE = 'drawing_vote'
    AVATAR = 'avatar'
    NAME = 'name'
    OWNER = 'owner'
    SETTING = 'setting'
    DRAW = 'draw'
    CLEAR = 'clear'
    UNDO = 'undo'
    START_ERROR = 'start_error'
    QUEUED_CHAT = 'queued_chat'
    PACKET = 'packet'


class GuessOutcome(StrEnum):
    INCORRECT = 'incorrect'
    CLOSE = 'close'
    CORRECT = 'correct'


class SettingID(IntEnum):
    LANGUAGE = 0
    SLOTS = 1
    DRAW_TIME = 2
    ROUNDS = 3
    WORD_COUNT = 4
    HINT_COUNT = 5
    WORD_MODE = 6
    CUSTOM_WORDS_ONLY = 7


@dataclass(frozen=True, slots=True)
class Player:
    id: int
    name: str = ''
    avatar: tuple[int, ...] = (0, 0, 0, -1)
    score: int = 0
    guessed: bool = False
    flags: int = 0


@dataclass(frozen=True, slots=True)
class Settings:
    raw: tuple[int, ...]

    @property
    def language(self) -> int:
        return self.raw[0]

    @property
    def slots(self) -> int:
        return self.raw[1]

    @property
    def draw_time(self) -> int:
        return self.raw[2]

    @property
    def rounds(self) -> int:
        return self.raw[3]

    @property
    def word_count(self) -> int:
        return self.raw[4]

    @property
    def hint_count(self) -> int:
        return self.raw[5]

    @property
    def word_mode(self) -> int:
        return self.raw[6]

    @property
    def custom_words_only(self) -> bool:
        return bool(self.raw[7])


@dataclass(frozen=True, slots=True)
class VoteKick:
    target_id: int
    current_votes: int
    required_votes: int
    last_voter_id: int
    observed_voters: frozenset[int]


@dataclass(frozen=True, slots=True)
class Snapshot:
    """State at one received packet; later packets produce new snapshots."""

    ready: bool = False
    lobby_id: str = ''
    lobby_type: int = 0
    me: int = -1
    owner: int = -1
    round: int = 0
    phase: int = Phase.WAITING
    turn_id: int = 0
    remaining_time: int = 0
    drawer_id: Optional[int] = None
    word: Optional[str] = None
    hint: Optional[str] = None
    word_choices: tuple[str, ...] = ()
    settings: Optional[Settings] = None
    players: Mapping[int, Player] = field(default_factory=lambda: MappingProxyType({}))
    vote_kick: Optional[VoteKick] = None

    @property
    def current_player(self) -> Optional[Player]:
        return self.players.get(self.me) if self.ready else None

    @property
    def drawer(self) -> Optional[Player]:
        return self.players.get(self.drawer_id) if self.drawer_id is not None else None

    @property
    def is_drawer(self) -> bool:
        return (
            self.ready
            and self.phase in (Phase.CHOOSING, Phase.DRAWING)
            and self.drawer_id == self.me
        )

    @property
    def drawing_enabled(self) -> bool:
        return self.is_drawer and self.phase == Phase.DRAWING

    @property
    def guessing_enabled(self) -> bool:
        player = self.current_player
        return (
            self.ready
            and self.phase == Phase.DRAWING
            and self.drawer_id is not None
            and self.drawer_id != self.me
            and player is not None
            and not player.guessed
        )


@dataclass(frozen=True, slots=True)
class SendReceipt:
    """Local confirmation of a socket write, not server acceptance or a guess result.

    ``sent_at`` uses monotonic time, not a wall-clock timestamp.
    """

    id: int
    text: str
    turn_id: int
    sent_at: float
    is_guess: bool = False


@dataclass(frozen=True, slots=True)
class GuessResponse:
    """An observed response correlated to a locally sent guess."""

    receipt: SendReceipt
    outcome: GuessOutcome

    @property
    def is_provisional(self) -> bool:
        """An echoed guess may still receive a following close notification."""
        return self.outcome == GuessOutcome.INCORRECT


@dataclass(frozen=True, slots=True)
class ChatMessage:
    player_id: int
    message: str


@dataclass(frozen=True, slots=True)
class GuessedUpdate:
    player_id: int
    word: Optional[str] = None


@dataclass(frozen=True, slots=True)
class HintLetter:
    index: int
    character: str


@dataclass(frozen=True, slots=True)
class QueuedChatResult:
    receipt: Optional[SendReceipt] = None
    error: Optional[Exception] = None


@dataclass(frozen=True, slots=True)
class Event:
    """Handlers receive one event; payload containers and snapshot are immutable."""

    name: str
    snapshot: Snapshot
    sequence: int
    received_at: float
    data: Any = None
    packet_id: Optional[int] = None
    guess_response: Optional[GuessResponse] = None
    previous_phase: Optional[int] = None
    turn_started: bool = False
    turn_ended: bool = False

    @property
    def chat(self) -> Optional[ChatMessage]:
        """Typed chat payload, or None for other events."""
        if self.packet_id == PacketID.CHAT:
            return ChatMessage(self.data['id'], self.data['msg'])
        return None

    @property
    def guessed(self) -> Optional[GuessedUpdate]:
        if self.packet_id == PacketID.GUESSED:
            return GuessedUpdate(self.data['id'], self.data.get('word'))
        return None

    @property
    def hint_letters(self) -> tuple[HintLetter, ...]:
        if self.packet_id != PacketID.HINT or not self.data:
            return ()
        pairs = (self.data,) if isinstance(self.data[0], int) else self.data
        return tuple(HintLetter(index, character) for index, character in pairs)

    @property
    def queued_chat(self) -> Optional[QueuedChatResult]:
        if self.name != EventName.QUEUED_CHAT:
            return None
        if isinstance(self.data, SendReceipt):
            return QueuedChatResult(receipt=self.data)
        if isinstance(self.data, Exception):
            return QueuedChatResult(error=self.data)
        return None


def random_avatar() -> tuple[int, int, int, int]:
    return (
        randrange(AVATAR_COLOR_VARIANTS),
        randrange(AVATAR_EYE_VARIANTS),
        randrange(AVATAR_MOUTH_VARIANTS),
        ORDINARY_AVATAR_SPECIAL,
    )


def freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(freeze(item) for item in value)
    return value
