"""Async skribbl.io client, decorator handlers, and immutable event models.

Register handlers with ``@client.on(EventName.CHAT)``, then explicitly connect
inside ``async with client``. The context manager closes resources on exit.
Optional image conversion lives in ``skribblpy.images``.
"""

from skribblpy.client import Client
from skribblpy.chat import TextSource
from skribblpy.events import EventHandler
from skribblpy.drawing import (
    PALETTE,
    DrawCommand,
    CANVAS_WIDTH,
    CANVAS_HEIGHT,
    DRAW_INTERVAL,
    DRAW_BATCH_SIZE,
)
from skribblpy.errors import (
    QueueFull,
    ImageError,
    ActionError,
    Disconnected,
    JoinRejected,
    SkribblError,
    ProtocolError,
    ConnectionFailed,
    MatchmakingError,
)
from skribblpy.models import (
    Event,
    Phase,
    Player,
    PacketID,
    Settings,
    Snapshot,
    VoteKick,
    EventName,
    SettingID,
    HintLetter,
    ChatMessage,
    SendReceipt,
    GuessOutcome,
    GuessResponse,
    GuessedUpdate,
    random_avatar,
    QueuedChatResult,
)

__all__ = [
    'Client',
    'TextSource',
    'EventHandler',
    'CANVAS_WIDTH',
    'CANVAS_HEIGHT',
    'DRAW_INTERVAL',
    'DRAW_BATCH_SIZE',
    'Event',
    'Phase',
    'Player',
    'Settings',
    'PacketID',
    'Snapshot',
    'VoteKick',
    'SendReceipt',
    'GuessResponse',
    'random_avatar',
    'PALETTE',
    'DrawCommand',
    'QueueFull',
    'ActionError',
    'Disconnected',
    'JoinRejected',
    'SkribblError',
    'ProtocolError',
    'ImageError',
    'ConnectionFailed',
    'MatchmakingError',
    'EventName',
    'SettingID',
    'HintLetter',
    'ChatMessage',
    'GuessOutcome',
    'GuessedUpdate',
    'QueuedChatResult',
]
