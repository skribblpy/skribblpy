"""State-checked gameplay actions shared by Client."""

from asyncio import sleep
from abc import ABC, abstractmethod
from collections.abc import Iterable
from typing import Any, TYPE_CHECKING
from skribblpy.errors import ActionError
from skribblpy.models import Phase, PacketID, Snapshot
from skribblpy.drawing import DrawCommand, DRAW_INTERVAL, DRAW_BATCH_SIZE

if TYPE_CHECKING:
    from skribblpy.images.data import ImageData


class Actions(ABC):
    @property
    @abstractmethod
    def snapshot(self) -> Snapshot:
        """Supply the authoritative state used to validate gameplay actions."""
        raise NotImplementedError

    @abstractmethod
    async def _send(self, packet_id: int, data: Any = ...) -> None:
        """Write a gameplay packet, omitting data when no value is supplied."""
        raise NotImplementedError

    async def set_setting(self, index: int, value: int) -> None:
        """Set a SettingID value as lobby owner; confirmation arrives in later state."""
        self._require_owner()
        if (
            not isinstance(index, int)
            or isinstance(index, bool)
            or not 0 <= index <= 7
            or type(value) is not int
        ):
            raise ValueError('Expected a setting index in 0..7 and integer value')
        await self._send(PacketID.SETTING, {'id': index, 'val': value})

    async def choose_word(self, *indices: int) -> None:
        """Choose one or two zero-based indices from snapshot.word_choices."""
        state = self.snapshot
        if not state.is_drawer or state.phase != Phase.CHOOSING or not state.word_choices:
            raise ActionError('Local player is not choosing a word')
        if not 1 <= len(indices) <= 2 or any(
            type(i) is not int or not 0 <= i < len(state.word_choices) for i in indices
        ):
            raise ValueError('Choose one or two offered word indices')
        await self._send(PacketID.CHOOSE_WORD, indices[0] if len(indices) == 1 else indices)

    async def start_game(self, custom_words: Iterable[str] = ()) -> None:
        """Start as owner; an optional word collection needs at least five nonempty words."""
        self._require_owner()
        if isinstance(custom_words, str):
            raise TypeError('Custom words must be a collection of strings')
        custom_words = tuple(custom_words)
        if any(not isinstance(word, str) for word in custom_words):
            raise TypeError('Custom words must be a collection of strings')
        words = tuple(word for item in custom_words if (word := item.strip()))
        if custom_words and (len(words) < 5 or any(',' in word for word in words)):
            raise ValueError('Provide at least five nonempty words without commas')
        await self._send(PacketID.START, ','.join(words))

    async def vote_drawing(self, like: bool) -> None:
        """Send a positive or negative drawing vote during an active turn."""
        if not self.snapshot.ready or self.snapshot.phase != Phase.DRAWING:
            raise ActionError('Drawing votes require an active drawing turn')
        await self._send(PacketID.VOTE, int(bool(like)))

    async def kick(self, player_id: int) -> None:
        """Kick a player present in the snapshot; requires ownership."""
        self._require_owner()
        self._require_player(player_id)
        await self._send(PacketID.KICK, player_id)

    async def ban(self, player_id: int) -> None:
        """Ban a player present in the snapshot; requires ownership."""
        self._require_owner()
        self._require_player(player_id)
        await self._send(PacketID.BAN, player_id)

    async def vote_kick(self, player_id: int) -> None:
        """Vote to remove a player other than the lobby owner."""
        self._require_player(player_id)
        if player_id == self.snapshot.owner:
            raise ActionError('The owner cannot be vote-kicked')
        await self._send(PacketID.VOTE_KICK, player_id)

    async def report(self, player_id: int, reasons: int) -> None:
        """Report a player with a bitmask combining reason bits 1, 2, and 4."""
        self._require_player(player_id)
        if type(reasons) is not int or not 1 <= reasons <= 7:
            raise ValueError('Report reasons must be a bitmask of 1, 2, and 4')
        await self._send(PacketID.REPORT, {'id': player_id, 'reasons': reasons})

    async def toggle_mute(self, player_id: int) -> None:
        """Toggle the server mute setting for a player present in the snapshot."""
        self._require_player(player_id)
        await self._send(PacketID.MUTE, player_id)

    async def draw(self, *commands: DrawCommand) -> None:
        """Send one batch of 1..8 commands as the active drawer; does not add pacing."""
        self._require_drawer()
        if not 1 <= len(commands) <= DRAW_BATCH_SIZE:
            raise ValueError('Draw batches contain 1..8 commands')
        if any(not isinstance(command, DrawCommand) for command in commands):
            raise TypeError('Expected validated DrawCommand objects')
        await self._send(PacketID.DRAW, [command.values for command in commands])

    async def send_drawing(self, commands: Iterable[DrawCommand]) -> None:
        """Send paced batches without clearing; stop with ActionError if the turn changes."""
        commands = tuple(commands)
        if any(not isinstance(command, DrawCommand) for command in commands):
            raise TypeError('Expected validated DrawCommand objects')
        turn = self.snapshot.turn_id
        for offset in range(0, len(commands), DRAW_BATCH_SIZE):
            if offset:
                await sleep(DRAW_INTERVAL)
            if turn != self.snapshot.turn_id:
                raise ActionError('Drawing turn changed during playback')
            await self.draw(*commands[offset : offset + DRAW_BATCH_SIZE])

    async def clear_drawing(self) -> None:
        """Clear the canvas as the active drawer."""
        self._require_drawer()
        await self._send(PacketID.CLEAR)

    async def undo_drawing(self, retained: int) -> None:
        """Keep the first retained commands, rather than removing that many commands."""
        self._require_drawer()
        if type(retained) is not int or retained < 0:
            raise ValueError('Retained command count must be nonnegative')
        await self._send(PacketID.UNDO, retained)

    async def send_image(self, image_data: 'ImageData') -> None:
        """Validate, clear the canvas, then send an image with normal drawing pacing."""
        image_data.validate()
        turn = self.snapshot.turn_id
        await self.clear_drawing()
        if turn != self.snapshot.turn_id:
            raise ActionError('Drawing turn changed during playback')
        await self.send_drawing(image_data.commands)

    def _require_owner(self):
        if not self.snapshot.ready or self.snapshot.me != self.snapshot.owner:
            raise ActionError('Action requires the lobby owner')

    def _require_player(self, player_id: int):
        if type(player_id) is not int:
            raise TypeError('Player ID must be an integer')
        if not self.snapshot.ready or player_id not in self.snapshot.players:
            raise ActionError('Player is not in the authoritative lobby snapshot')

    def _require_drawer(self):
        if not self.snapshot.drawing_enabled:
            raise ActionError('Action requires the active drawer')
