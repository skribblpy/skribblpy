"""Independent Pillow drawing oracle for development validation."""

from functools import lru_cache
from PIL.Image import open as open_image
from PIL.ImageDraw import Draw, floodfill
from PIL.Image import new, Image, frombytes
from skribblpy.images.data import ImageData
from numpy import uint8, arange, repeat, uint32, asarray
from skribblpy.images.quantize import yliluoma, cluster_dot
from skribblpy._image_native import SAMPLE_WIDTH, SAMPLE_HEIGHT
from skribblpy.drawing import PALETTE, CANVAS_WIDTH, CANVAS_HEIGHT

GRAY16_MODES = frozenset(('I;16', 'I;16L', 'I;16B', 'I;16N'))


@lru_cache(maxsize=37)
def _brush_mask(diameter):
    radius = diameter // 2
    values = bytes(
        255 if 4 * (x * x + y * y) < diameter * diameter else 0
        for y in range(-radius, diameter - radius)
        for x in range(-radius, diameter - radius)
    )
    return frombytes('L', (diameter, diameter), values)


def render_preview(data: ImageData) -> Image:
    """Replay commands to a Pillow RGB image. Browser antialiasing may differ."""
    data.validate()
    canvas = new('RGB', (CANVAS_WIDTH, CANVAS_HEIGHT), PALETTE[0])
    painter = Draw(canvas)
    for command in data.commands:
        values = command.values
        color = PALETTE[values[1]]
        if values[0] == 1:
            # Palette entries differ by more than the flood-fill tolerance.
            floodfill(canvas, (values[2], values[3]), color)
            continue
        _, _, diameter, x0, y0, x1, y1 = values
        radius = diameter // 2
        mask = _brush_mask(diameter)
        # Merging stamps into rectangles makes generated scanlines very cheap.
        if y0 == y1 or x0 == x1:
            for delta in range(-radius, diameter - radius):
                extent = 0
                while 4 * ((extent + 1) ** 2 + delta**2) < diameter**2:
                    extent += 1
                if 4 * delta**2 >= diameter**2:
                    continue
                if y0 == y1:
                    painter.line(
                        (min(x0, x1) - extent, y0 + delta, max(x0, x1) + extent, y0 + delta),
                        fill=color,
                    )
                else:
                    painter.line(
                        (x0 + delta, min(y0, y1) - extent, x0 + delta, max(y0, y1) + extent),
                        fill=color,
                    )
            continue
        dx, dy = abs(x1 - x0), -abs(y1 - y0)
        sx, sy = (1 if x0 < x1 else -1), (1 if y0 < y1 else -1)
        error = dx + dy
        while True:
            canvas.paste(
                color,
                (x0 - radius, y0 - radius, x0 - radius + diameter, y0 - radius + diameter),
                mask,
            )
            if x0 == x1 and y0 == y1:
                break
            doubled = 2 * error
            if doubled >= dy:
                error += dy
                x0 += sx
            if doubled <= dx:
                error += dx
                y0 += sy
    return canvas


def generate_image(source, *, preset='cluster-dot'):
    # Check the internal algorithm directly.
    # noinspection PyProtectedMember
    from skribblpy.images.generate import _runs, _run_count

    if not isinstance(source, Image):
        with open_image(source) as loaded:
            return generate_image(loaded, preset=preset)
    width, height = source.size
    x = ((2 * arange(SAMPLE_WIDTH) + 1) * width) // (2 * SAMPLE_WIDTH)
    y = ((2 * arange(SAMPLE_HEIGHT) + 1) * height) // (2 * SAMPLE_HEIGHT)
    if source.mode in GRAY16_MODES:
        gray = asarray(source)[y[:, None], x[None, :]].astype(uint32)
        pixels = repeat(((gray + 128) // 257)[..., None], 3, axis=-1).astype(uint8)
    else:
        rgba = asarray(source.convert('RGBA'))[y[:, None], x[None, :]].astype(uint32)
        alpha = rgba[..., 3:4]
        pixels = ((rgba[..., :3] * alpha + 255 * (255 - alpha) + 127) // 255).astype(uint8)
    grid = yliluoma(pixels) if preset == 'yliluoma-1' else cluster_dot(pixels)
    vertical = _run_count(grid, vertical=True) < _run_count(grid, vertical=False)
    return ImageData(_runs(grid, vertical=vertical), width, height, width, height, preset=preset)
