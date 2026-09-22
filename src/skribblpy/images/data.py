"""Drawing file metadata, validation, and asynchronous storage."""

from pathlib import Path
from asyncio import to_thread
from json import dumps, loads
from dataclasses import dataclass
from skribblpy.errors import ProtocolError
from skribblpy._image_native import MAX_SOURCE_PIXELS
from skribblpy.constants import MAX_BRUSH_DIAMETER, MIN_BRUSH_DIAMETER, MAX_DRAWING_HISTORY
from skribblpy.drawing import (
    DrawCommand,
    CANVAS_WIDTH,
    CANVAS_HEIGHT,
    DRAW_INTERVAL,
    DRAW_BATCH_SIZE,
)

IMAGE_DATA_VERSION = 1
MAX_IMAGE_COMMANDS = MAX_DRAWING_HISTORY
MAX_IMAGE_FILE_BYTES = 64 << 20
DITHER_PRESETS = ('cluster-dot', 'yliluoma-1')
PRESETS = (*DITHER_PRESETS, 'optimized')


@dataclass(frozen=True, slots=True)
class ImageData:
    """Drawing commands plus source metadata; validate before sending or saving."""

    commands: tuple[DrawCommand, ...]
    source_width: int
    source_height: int
    crop_width: int
    crop_height: int
    crop_x: int = 0
    crop_y: int = 0
    canvas_offset_x: int = 0
    canvas_offset_y: int = 0
    brush_diameter: int = MIN_BRUSH_DIAMETER
    version: int = IMAGE_DATA_VERSION
    fit_mode: str = 'stretch'
    preset: str = 'cluster-dot'

    def __post_init__(self) -> None:
        object.__setattr__(self, 'commands', tuple(self.commands))

    @property
    def estimated_send_duration(self) -> float:
        """Pacing delays between batches, excluding network and server latency."""
        return max(0, (len(self.commands) - 1) // DRAW_BATCH_SIZE) * DRAW_INTERVAL

    def validate(self) -> None:
        if self.version != IMAGE_DATA_VERSION or self.preset not in PRESETS:
            raise ProtocolError('Unsupported image version or preset')
        numbers = (
            self.source_width,
            self.source_height,
            self.crop_width,
            self.crop_height,
            self.crop_x,
            self.crop_y,
            self.canvas_offset_x,
            self.canvas_offset_y,
            self.brush_diameter,
            self.version,
        )
        if any(type(value) is not int for value in numbers):
            raise ProtocolError('Image dimensions and version must be integers')
        if min(self.source_width, self.source_height, self.crop_width, self.crop_height) <= 0:
            raise ProtocolError('Invalid image dimensions')
        if self.source_width * self.source_height > MAX_SOURCE_PIXELS:
            raise ProtocolError('Source exceeds image pixel limit')
        if (
            min(self.crop_x, self.crop_y) < 0
            or self.crop_x + self.crop_width > self.source_width
            or self.crop_y + self.crop_height > self.source_height
        ):
            raise ProtocolError('Crop is outside source')
        if self.fit_mode == 'stretch':
            if (
                self.crop_x
                or self.crop_y
                or self.crop_width != self.source_width
                or self.crop_height != self.source_height
                or self.canvas_offset_x
                or self.canvas_offset_y
            ):
                raise ProtocolError('Stretched images must cover the source and canvas')
        elif self.fit_mode == 'center_crop':
            if (
                min(self.canvas_offset_x, self.canvas_offset_y) < 0
                or self.canvas_offset_x + self.crop_width > CANVAS_WIDTH
                or self.canvas_offset_y + self.crop_height > CANVAS_HEIGHT
            ):
                raise ProtocolError('Crop is outside canvas')
        else:
            raise ProtocolError('Unsupported image fit mode')
        if (
            not MIN_BRUSH_DIAMETER <= self.brush_diameter <= MAX_BRUSH_DIAMETER
            or len(self.commands) > MAX_IMAGE_COMMANDS
        ):
            raise ProtocolError('Invalid image brush diameter or command count')
        for command in self.commands:
            if not isinstance(command, DrawCommand):
                raise ProtocolError('Expected validated drawing commands')
            if command.values[0] == 0:
                _, _, margin, x0, y0, x1, y1 = command.values
                if not (
                    min(x0, x1) >= -margin
                    and max(x0, x1) <= CANVAS_WIDTH + margin
                    and min(y0, y1) >= -margin
                    and max(y0, y1) <= CANVAS_HEIGHT + margin
                ):
                    raise ProtocolError('Image brush command exceeds bounded canvas margin')

    async def save(self, path: str | Path) -> None:
        """Validate and write drawing JSON off the event loop.

        An already running write may finish after cancellation.
        """
        await to_thread(self._save, path)

    @classmethod
    async def load(cls, path: str | Path) -> 'ImageData':
        """Read drawing JSON off the event loop; malformed data raises ProtocolError."""
        return await to_thread(cls._load, path)

    def _save(self, path: str | Path) -> None:
        """Validate and write compact drawing JSON; filesystem errors propagate."""
        self.validate()
        values = {wire: getattr(self, local) for wire, local in _FIELDS.items()}
        values['commands'] = [command.values for command in self.commands]
        Path(path).write_text(dumps(values, separators=(',', ':')), encoding='utf-8')

    @classmethod
    def _load(cls, path: str | Path) -> 'ImageData':
        """Load validated drawing JSON; malformed data raises ProtocolError."""
        with Path(path).open('rb') as source:
            raw = source.read(MAX_IMAGE_FILE_BYTES + 1)
        if len(raw) > MAX_IMAGE_FILE_BYTES:
            raise ProtocolError('Image file exceeds size limit')
        try:
            data = loads(raw)
            if not isinstance(data, dict) or set(data) - set(_FIELDS) - {'commands'}:
                raise ValueError('Unexpected image data fields')
            values = {local: data[wire] for wire, local in _FIELDS.items() if wire in data}
            values['version'] = data['version']
            values['fit_mode'] = data.get('fitMode') or 'center_crop'
            values['preset'] = data.get('preset') or PRESETS[0]
            values['commands'] = tuple(DrawCommand(tuple(command)) for command in data['commands'])
            result = cls(**values)
            result.validate()
            return result
        except (ValueError, KeyError, TypeError) as error:
            raise ProtocolError(f'Invalid image data: {error}') from error


_FIELDS = {
    'sourceWidth': 'source_width',
    'sourceHeight': 'source_height',
    'cropWidth': 'crop_width',
    'cropHeight': 'crop_height',
    'cropX': 'crop_x',
    'cropY': 'crop_y',
    'canvasOffsetX': 'canvas_offset_x',
    'canvasOffsetY': 'canvas_offset_y',
    'brushDiameter': 'brush_diameter',
    'version': 'version',
    'fitMode': 'fit_mode',
    'preset': 'preset',
}
