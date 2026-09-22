"""Image sampling and horizontal/vertical brush-run compression."""

from pathlib import Path
from typing import Optional
from asyncio import to_thread
from skribblpy.errors import ImageError
from skribblpy.drawing import DrawCommand
from skribblpy.images.raster import RasterImage
from skribblpy.images.data import PRESETS, ImageData
from skribblpy.images.quantize import yliluoma, cluster_dot
from numpy import array, uint8, arange, frombuffer, concatenate, flatnonzero
from skribblpy._image_native import sample_path, SAMPLE_WIDTH, SAMPLE_HEIGHT, sample_buffer

CELL_SIZE = 4
STROKE_PITCH = 3


def _run_count(grid, *, vertical: bool) -> int:
    if vertical:
        grid = grid.T
    sampled = grid[arange(0, grid.shape[0] * CELL_SIZE, STROKE_PITCH) // CELL_SIZE]
    return int(
        (sampled[:, 0] != 0).sum()
        + ((sampled[:, 1:] != sampled[:, :-1]) & (sampled[:, 1:] != 0)).sum()
    )


def _runs(grid, *, vertical: bool):
    if vertical:
        grid = grid.T
    commands = []
    for origin in range(0, grid.shape[0] * CELL_SIZE, STROKE_PITCH):
        row = grid[origin // CELL_SIZE]
        boundaries = concatenate(
            (array([0]), flatnonzero(row[1:] != row[:-1]) + 1, array([len(row)]))
        )
        for start, end in zip(boundaries[:-1], boundaries[1:], strict=True):
            color = int(row[start])
            if color:
                low, high, line = int(start) * CELL_SIZE, int(end) * CELL_SIZE, origin + 1
                coordinates = (line, low, line, high) if vertical else (low, line, high, line)
                commands.append(DrawCommand.brush(color, CELL_SIZE, *coordinates))
    return tuple(commands)


def _sample(source: RasterImage | str | Path):
    try:
        if isinstance(source, RasterImage):
            width, height = source.size
            raw = sample_buffer(source.pixels, width, height, source.mode)
        else:
            width, height, raw = sample_path(source)
    except ValueError as error:
        raise ImageError(str(error)) from error
    pixels = frombuffer(raw, dtype=uint8).reshape(SAMPLE_HEIGHT, SAMPLE_WIDTH, 3)
    return width, height, pixels


def _generate_image(
    source: RasterImage | str | Path,
    *,
    preset: str = 'cluster-dot',
    max_commands: Optional[int] = None,
    max_duration: Optional[float] = None,
) -> ImageData:
    if preset not in PRESETS:
        raise ValueError(f'Unknown image preset: {preset}')
    if preset == 'optimized':
        from skribblpy.images.optimize import _optimize_image

        return _optimize_image(source, max_commands=max_commands, max_duration=max_duration).image
    if max_commands is not None or max_duration is not None:
        raise ValueError('Drawing budgets require preset=optimized')
    width, height, pixels = _sample(source)
    grid = yliluoma(pixels) if preset == 'yliluoma-1' else cluster_dot(pixels)
    vertical = _run_count(grid, vertical=True) < _run_count(grid, vertical=False)
    commands = _runs(grid, vertical=vertical)
    result = ImageData(commands, width, height, width, height, preset=preset)
    result.validate()
    return result


async def generate_image(
    source: RasterImage | str | Path,
    *,
    preset: str = 'cluster-dot',
    max_commands: Optional[int] = None,
    max_duration: Optional[float] = None,
) -> ImageData:
    """Convert an image off the event loop. GIF uses frame zero.

    Budgets require preset='optimized' and default to unlimited within protocol
    limits. Cancellation stops waiting; an already running worker may finish.
    """
    return await to_thread(
        _generate_image,
        source,
        preset=preset,
        max_commands=max_commands,
        max_duration=max_duration,
    )
