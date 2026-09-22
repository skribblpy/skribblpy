"""Exceptions raised by the protocol and client."""


class SkribblError(Exception):
    """Base framework error."""


class ProtocolError(SkribblError, ValueError):
    """A wire frame, drawing command, or saved drawing payload is invalid."""


class ImageError(SkribblError, ValueError):
    """Image pixels, encoded input, or image options are invalid."""


class Disconnected(SkribblError):
    """The transport ended or has not connected."""


class ConnectionFailed(Disconnected):
    """HTTP/WebSocket setup failed; the original error is chained as its cause."""


class MatchmakingError(ConnectionFailed):
    """The matchmaker returned an unsuccessful HTTP status."""

    def __init__(self, status: int) -> None:
        self.status = status
        super().__init__(f'Matchmaking returned HTTP {status}')


class QueueFull(SkribblError):
    """A bounded queue cannot accept more work."""


class ActionError(SkribblError):
    """The current authoritative state does not permit an action."""


class JoinRejected(SkribblError):
    """A server join rejection, retaining its numeric code."""

    def __init__(self, code: int) -> None:
        self.code = code
        descriptions = {
            1: 'room not found',
            2: 'room full',
            3: 'kick cooldown',
            4: 'banned',
            5: 'joining too quickly',
            100: 'already connected',
            200: 'IP connection limit',
            300: 'too many kicks',
        }
        super().__init__(f'Join rejected ({code}): {descriptions.get(code, "unknown reason")}')
