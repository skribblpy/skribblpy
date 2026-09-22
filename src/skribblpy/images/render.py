"""Native deterministic brush and connected-fill replay."""

from asyncio import to_thread
from skribblpy._image_native import render
from skribblpy.errors import ProtocolError
from skribblpy.images.data import ImageData
from skribblpy.images.raster import RasterImage
from skribblpy.drawing import PALETTE, CANVAS_WIDTH, CANVAS_HEIGHT


def _render_preview(data: ImageData) -> RasterImage:
    """Replay commands to immutable RGB pixels. Browser antialiasing may differ."""
    data.validate()
    try:
        pixels = render([command.values for command in data.commands], PALETTE)
    except ValueError as error:
        raise ProtocolError(str(error)) from error
    return RasterImage(CANVAS_WIDTH, CANVAS_HEIGHT, pixels)


async def render_preview(data: ImageData) -> RasterImage:
    """Replay commands off the event loop. Browser antialiasing may differ."""
    return await to_thread(_render_preview, data)
