"""Validated wire commands and the fixed 800 by 600 canvas palette."""

from dataclasses import dataclass
from skribblpy.errors import ProtocolError
from skribblpy.constants import MAX_BRUSH_DIAMETER, MIN_BRUSH_DIAMETER

CANVAS_WIDTH = 800
CANVAS_HEIGHT = 600
DRAW_BATCH_SIZE = 8
DRAW_INTERVAL = 0.033
PALETTE = (
    (255, 255, 255),
    (0, 0, 0),
    (193, 193, 193),
    (80, 80, 80),
    (239, 19, 11),
    (116, 11, 7),
    (255, 113, 0),
    (194, 56, 0),
    (255, 228, 0),
    (232, 162, 0),
    (0, 204, 0),
    (0, 70, 25),
    (0, 255, 145),
    (0, 120, 93),
    (0, 178, 255),
    (0, 86, 158),
    (35, 31, 211),
    (14, 8, 101),
    (163, 0, 186),
    (85, 0, 105),
    (223, 105, 167),
    (135, 53, 84),
    (255, 172, 142),
    (204, 119, 77),
    (160, 82, 45),
    (99, 48, 13),
)


@dataclass(frozen=True, slots=True)
class DrawCommand:
    values: tuple[int, ...]

    def __post_init__(self):
        values = tuple(self.values)
        object.__setattr__(self, 'values', values)
        if any(type(value) is not int for value in values):
            raise ProtocolError('Draw command values must be integers')
        if not values or (values[0], len(values)) not in ((0, 7), (1, 4)):
            raise ProtocolError('Invalid drawing tool or arity')
        if not 0 <= values[1] < len(PALETTE):
            raise ProtocolError('Invalid palette index')
        if values[0] == 0 and not MIN_BRUSH_DIAMETER <= values[2] <= MAX_BRUSH_DIAMETER:
            raise ProtocolError('Brush diameter must be 4..40')
        if values[0] == 1 and not (
            0 <= values[2] < CANVAS_WIDTH and 0 <= values[3] < CANVAS_HEIGHT
        ):
            raise ProtocolError('Fill seed is outside the canvas')

    @classmethod
    def brush(cls, color: int, diameter: int, x0: int, y0: int, x1: int, y1: int) -> 'DrawCommand':
        return cls((0, color, diameter, x0, y0, x1, y1))

    @classmethod
    def fill(cls, color: int, x: int = 0, y: int = 0) -> 'DrawCommand':
        return cls((1, color, x, y))
