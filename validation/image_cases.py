"""Deterministic fixtures for image conversion and drawing replay."""

from skribblpy.images import ImageData
from skribblpy import PALETTE, DrawCommand
from PIL.Image import new, Transpose, fromarray
from numpy import stack, uint8, arange, uint16, indices
from skribblpy.constants import MAX_BRUSH_DIAMETER, MIN_BRUSH_DIAMETER


def conversion_cases():
    x, y = indices((37, 53), dtype=uint16)[::-1]
    colors = stack((x * 5 % 256, y * 7 % 256, (x * 17 + y * 29) % 256), axis=-1)
    yield 'odd-rgb', fromarray(colors.astype(uint8))
    alpha = (x * 13 + y * 19) % 256
    yield 'alpha-ramp', fromarray(stack((*colors.transpose(2, 0, 1), alpha), axis=-1).astype(uint8))
    yield 'transparent', new('RGBA', (13, 7), (37, 199, 86, 0))
    yield 'single-pixel', new('RGBA', (1, 1), (113, 7, 201, 127))
    yield 'single-row', fromarray(colors[:1].astype(uint8))
    yield 'single-column', fromarray(colors[:, :1].astype(uint8))
    yield 'gray-ramp', fromarray(arange(256, dtype=uint8)[None, :])
    yield 'gray16-ramp', fromarray((arange(257, dtype=uint16) * 255)[None, :])
    yield 'gray-alpha', fromarray(stack((colors[..., 0], alpha), axis=-1).astype(uint8))
    x, y = indices((151, 201), dtype=uint16)[::-1]
    yield (
        'gradient',
        fromarray(
            stack((x * 255 // 200, y * 255 // 150, (x + y) * 255 // 350), axis=-1).astype(uint8)
        ),
    )
    palette = new('RGB', (26, 1))
    palette.putdata(PALETTE)
    yield 'palette', palette
    yield 'palette-vertical', palette.transpose(Transpose.ROTATE_90)
    # Midpoints exercise nearest-color ties and ordered dither thresholds.
    midpoints = new('RGB', (26, 26))
    midpoints.putdata(
        [
            tuple((a + b) // 2 for a, b in zip(left, right, strict=True))
            for left in PALETTE
            for right in PALETTE
        ]
    )
    yield 'palette-midpoints', midpoints
    x, y = indices((601, 803), dtype=uint16)[::-1]
    yield 'checkerboard-downsample', fromarray(((x + y) % 2 * 255).astype(uint8))


def drawing_cases():
    commands = []
    for size in range(MIN_BRUSH_DIAMETER, MAX_BRUSH_DIAMETER + 1):
        x, y = 45 + (size - 4) % 10 * 78, 45 + (size - 4) // 10 * 140
        color = 1 + size % (len(PALETTE) - 1)
        commands.extend(
            (
                DrawCommand.brush(color, size, x - 25, y - 20, x + 25, y + 20),
                DrawCommand.brush(color, size, x + 25, y + 40, x - 25, y + 40),
                DrawCommand.brush(color, size, x, y + 90, x, y + 50),
                DrawCommand.brush(color, size, x + 25, y + 75, x + 25, y + 75),
            )
        )
    yield 'all-brush-sizes', ImageData(tuple(commands), 800, 600, 800, 600)
    commands = []
    endpoints = (
        (-20, -20, 820, 620),
        (820, -20, -20, 620),
        (0, 600, 800, 0),
        (800, 0, 0, 600),
        (-20, 0, 820, 0),
        (800, 620, 800, -20),
    )
    for index, points in enumerate(endpoints):
        commands.append(DrawCommand.brush(index + 1, 40, *points))
    yield 'clipped-reversed', ImageData(tuple(commands), 800, 600, 800, 600)
    commands = []
    for index, (dx, dy) in enumerate(
        (
            (170, 60),
            (60, 170),
            (-60, 170),
            (-170, 60),
            (-170, -60),
            (-60, -170),
            (60, -170),
            (170, -60),
        )
    ):
        commands.append(DrawCommand.brush(index + 2, 5 + index, 400, 300, 400 + dx, 300 + dy))
    yield 'eight-octants', ImageData(tuple(commands), 800, 600, 800, 600)
    commands = (
        DrawCommand.brush(1, 4, 100, 100, 700, 100),
        DrawCommand.brush(1, 4, 700, 100, 700, 500),
        DrawCommand.brush(1, 4, 700, 500, 100, 500),
        DrawCommand.brush(1, 4, 100, 500, 100, 100),
        DrawCommand.brush(1, 5, 400, 100, 400, 500),
        DrawCommand.fill(4, 200, 200),
        DrawCommand.fill(4, 200, 200),
        DrawCommand.fill(14, 600, 200),
        DrawCommand.fill(8, 0, 0),
        DrawCommand.brush(0, 12, 400, 100, 400, 500),
        DrawCommand.fill(12, 400, 300),
        DrawCommand.fill(3, 799, 599),
    )
    yield 'connected-fill', ImageData(commands, 800, 600, 800, 600)
