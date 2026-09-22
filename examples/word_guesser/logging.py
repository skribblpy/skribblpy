"""Console formatting and game-event logging for word guesser workers."""

from os import environ
from skribblpy import Phase, Snapshot, EventName
from logging import (
    INFO,
    ERROR,
    WARNING,
    Formatter,
    getLogger,
    basicConfig,
    LoggerAdapter,
    StreamHandler,
)

COLORS = {
    'error': '\x1b[31m',
    'success': '\x1b[32m',
    'warning': '\x1b[33m',
    'player': '\x1b[34m',
    'join': '\x1b[36m',
    'chat': '\x1b[90m',
}
COLOR_RESET = '\x1b[0m'
ACTIVE_PHASES = frozenset((Phase.STARTING, Phase.ROUND, Phase.CHOOSING, Phase.DRAWING))


class ConsoleFormatter(Formatter):
    def __init__(self, *, color=False):
        super().__init__('%(asctime)s %(message)s', datefmt='%H:%M:%S')
        self.color = color

    def format(self, record):
        message = super().format(record)
        if not self.color:
            return message
        category = getattr(record, 'color', '')
        if record.levelno >= ERROR:
            category = 'error'
        elif record.levelno >= WARNING:
            category = 'warning'
        color = COLORS.get(category, '')
        return f'{color}{message}{COLOR_RESET}' if color else message


class SessionLogger(LoggerAdapter):
    def __init__(self, worker, session):
        super().__init__(getLogger(f'guesser.{worker:02d}'), {})
        self.prefix = f'[Bot {worker:02d}] [Session {session}]'

    def process(self, msg, kwargs):
        return f'{self.prefix} {msg}', kwargs


class EventLogger:
    def __init__(self, logger):
        self.logger = logger
        self.previous = Snapshot()
        self.waiting_for_restart = False

    def log(self, event):
        if event.name == EventName.QUEUED_CHAT:
            return
        state, previous = event.snapshot, self.previous
        if event.name == EventName.PLAYER_JOIN:
            self.logger.info(
                '%s joined the lobby.',
                player_label(state, event.data['id']),
                extra={'color': 'join'},
            )
        elif event.name == EventName.PLAYER_LEAVE:
            self.logger.info(
                '%s left the lobby.',
                player_label(previous, event.data['id']),
                extra={'color': 'warning'},
            )
        elif event.name == EventName.CHAT:
            self.logger.info(
                'Chat | %s: %r',
                player_label(state, event.data['id']),
                event.data['msg'],
                extra={'color': 'chat'},
            )
        elif event.name == EventName.GUESSED and event.data['id'] != state.me:
            self.logger.info(
                '%s guessed correctly.',
                player_label(state, event.data['id']),
                extra={'color': 'player'},
            )
        elif event.name == EventName.SPAM:
            self.logger.warning('Server requested a chat cooldown; slowing down queued messages.')

        if event.turn_ended:
            self.logger.info(
                'Turn %d ended (round %d). Answer: %s.',
                previous.turn_id if previous.ready else state.turn_id,
                state.round,
                repr(state.word) if state.word else 'not revealed',
            )
        if event.name == EventName.STATE:
            if (
                previous.ready
                and previous.phase == Phase.TURN_RESULT
                and state.phase in (Phase.ROUND, Phase.GAME_RESULT)
            ):
                self.logger.info('Round %d finished.', previous.round)
            if self.waiting_for_restart and state.phase in ACTIVE_PHASES:
                self.logger.info('New game started at round %d.', state.round)
                self.waiting_for_restart = False
            if state.phase == Phase.GAME_RESULT and not self.waiting_for_restart:
                placements = event.data.get('data') if event.data else None
                if isinstance(placements, (list, tuple)):
                    for placement in placements:
                        if (
                            isinstance(placement, (list, tuple))
                            and len(placement) >= 2
                            and placement[0] == state.me
                            and type(placement[1]) is int
                            and placement[1] >= 0
                        ):
                            self.logger.info(
                                'Match finished in place %d.',
                                placement[1] + 1,
                                extra={'color': 'success' if placement[1] == 0 else 'player'},
                            )
                            break
                self.waiting_for_restart = True
        elif event.name == EventName.LOBBY and state.phase == Phase.GAME_RESULT:
            self.waiting_for_restart = True
        self.previous = state


def configure_logging():
    handler = StreamHandler()
    color = handler.stream.isatty() and 'NO_COLOR' not in environ and environ.get('TERM') != 'dumb'
    handler.setFormatter(ConsoleFormatter(color=color))
    basicConfig(level=INFO, handlers=[handler])


def player_label(state, player_id):
    player = state.players.get(player_id)
    return f'{player.name!r} ({player.id})' if player else f'player {player_id}'
