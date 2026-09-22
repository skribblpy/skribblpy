"""Rendered quality and exact DRAW event payload measurements."""

from json import dumps
from asyncio import to_thread
from skribblpy.models import PacketID
from skribblpy.images.data import ImageData
from numpy import uint8, float64, frombuffer
from skribblpy.images.quantize import _features
from skribblpy.images.raster import RasterImage
from skribblpy._image_native import composite_rgb
from skribblpy.drawing import CANVAS_WIDTH, CANVAS_HEIGHT, DRAW_BATCH_SIZE

QUALITY_SCALES = (1, 8, 20)
QUALITY_WEIGHTS = (0.1, 0.35, 0.55)


def _drawing_payload_bytes(data: ImageData) -> int:
    """Size of paced Socket.IO DRAW frames, excluding clear and WebSocket/TLS framing."""
    total = 0
    for start in range(0, len(data.commands), DRAW_BATCH_SIZE):
        packet = [
            'data',
            {
                'id': PacketID.DRAW,
                'data': [
                    command.values for command in data.commands[start : start + DRAW_BATCH_SIZE]
                ],
            },
        ]
        total += len(
            (
                '42' + dumps(packet, ensure_ascii=False, separators=(',', ':'), allow_nan=False)
            ).encode('utf-8')
        )
    return total


def _quality_features(image: RasterImage):
    if image.size != (CANVAS_WIDTH, CANVAS_HEIGHT):
        raise ValueError('Quality comparison requires canvas-sized images')
    rgb = (
        image.pixels
        if image.mode == 'RGB'
        else composite_rgb(image.pixels, image.width, image.height, image.mode)
    )
    pixels = frombuffer(rgb, dtype=uint8).reshape(CANVAS_HEIGHT, CANVAS_WIDTH, 3).astype(float64)
    return tuple(
        _features(
            pixels
            if scale == 1
            else pixels.reshape(
                CANVAS_HEIGHT // scale, scale, CANVAS_WIDTH // scale, scale, 3
            ).mean(axis=(1, 3))
        )
        for scale in QUALITY_SCALES
    )


def _error_from_features(target_features, rendered: RasterImage) -> float:
    result = 0.0
    for target, actual, weight in zip(
        target_features, _quality_features(rendered), QUALITY_WEIGHTS, strict=True
    ):
        result += weight * float(((target - actual) ** 2).sum(axis=-1).mean())
    return result


def _visual_error(target: RasterImage, rendered: RasterImage) -> float:
    """Multiscale palette/luminance error, lower is better; not a perceptual guarantee.

    Both images must be canvas images. Fine detail contributes alongside
    local color averages, so dense dithering does not dominate the comparison.
    """
    return _error_from_features(_quality_features(target), rendered)


async def drawing_payload_bytes(data: ImageData) -> int:
    """Measure DRAW event JSON off the event loop, excluding clear and wire framing."""
    return await to_thread(_drawing_payload_bytes, data)


async def visual_error(target: RasterImage, rendered: RasterImage) -> float:
    """Measure multiscale canvas color/luminance error off the event loop."""
    return await to_thread(_visual_error, target, rendered)
