from json import dumps
from numpy import array
from PIL.Image import new
from hashlib import sha256
from pytest import mark, raises
from skribblpy.images.quantize import yliluoma
from skribblpy import PALETTE, DrawCommand, ProtocolError
from skribblpy.images import ImageData, RasterImage, generate_image, render_preview


@mark.parametrize('preset', ['cluster-dot', 'yliluoma-1'])
async def test_conversion_covers_full_canvas_with_valid_geometry(preset, tmp_path):
    source = new('RGB', (4, 2), PALETTE[4])
    source.paste(PALETTE[1], (2, 0, 4, 2))
    data = await generate_image((await RasterImage.from_pillow(source)), preset=preset)
    assert len(data.commands) == 267
    assert data.commands[0].values == (0, 4, 4, 1, 0, 1, 600)
    assert data.commands[134].values == (0, 1, 4, 403, 0, 403, 600)
    image = await render_preview(data)
    assert image.size == (800, 600)
    assert image.getpixel((20, 580)) == PALETTE[4]
    assert image.getpixel((780, 580)) == PALETTE[1]
    path = tmp_path / 'drawing.json'
    await data.save(path)
    assert (await ImageData.load(path)) == data


async def test_transparency_and_uniform_canvas():
    assert not (
        await generate_image(RasterImage(4, 4, bytes((255, 0, 0, 0)) * 16, 'RGBA'))
    ).commands
    data = await generate_image(RasterImage(1, 1, bytes(PALETTE[4])))
    image = await render_preview(data)
    assert image.tobytes() == bytes(PALETTE[4]) * 800 * 600
    assert data.estimated_send_duration == 24 * 0.033


async def test_brush_render_matches_independent_integer_stamping():
    for diameter in (4, 5, 12, 39, 40):
        commands = (
            DrawCommand.brush(4, diameter, 40, 40, 70, 40),
            DrawCommand.brush(1, diameter, 60, 10, 60, 80),
            DrawCommand.brush(14, diameter, 90, 90, 110, 120),
        )
        actual = await render_preview(ImageData(commands, 800, 600, 800, 600))
        expected = new('RGB', (800, 600), PALETTE[0])
        pixels = expected.load()
        assert pixels is not None
        for command in commands:
            _, color, size, x, y, end_x, end_y = command.values
            dx, dy = abs(end_x - x), -abs(end_y - y)
            sx, sy = (1 if x < end_x else -1), (1 if y < end_y else -1)
            error = dx + dy
            while True:
                for oy in range(-size // 2 + size % 2, size - size // 2):
                    for ox in range(-size // 2 + size % 2, size - size // 2):
                        if (
                            4 * (ox * ox + oy * oy) < size * size
                            and 0 <= x + ox < 800
                            and 0 <= y + oy < 600
                        ):
                            pixels[x + ox, y + oy] = PALETTE[color]
                if (x, y) == (end_x, end_y):
                    break
                doubled = 2 * error
                if doubled >= dy:
                    error += dy
                    x += sx
                if doubled <= dx:
                    error += dx
                    y += sy
        assert actual.tobytes() == expected.tobytes()


async def test_fill_is_connected_and_ordered():
    commands = (DrawCommand.brush(1, 4, 400, 0, 400, 600), DrawCommand.fill(4, 0, 0))
    image = await render_preview(ImageData(commands, 800, 600, 800, 600))
    assert image.getpixel((0, 0)) == PALETTE[4]
    assert image.getpixel((799, 0)) == PALETTE[0]
    assert image.getpixel((400, 0)) == PALETTE[1]


def test_yliluoma_selection_matches_scalar_distance():
    # Check the internal algorithm directly.
    # noinspection PyProtectedMember
    from skribblpy.images.quantize import _features, _mixing_plans

    colors = array([[[80, 120, 160], [145, 30, 90], [10, 20, 30]]])
    plans, penalties, selections = _mixing_plans()
    actual = yliluoma(colors)
    for position, color in enumerate(colors[0]):
        target = _features(color)
        best = min(
            range(len(plans)),
            key=lambda index: (
                sum((float(target[axis]) - float(plans[index, axis])) ** 2 for axis in range(4))
                + penalties[index]
            ),
        )
        first, second, ratio = selections[best]
        threshold = (0, 48 / 64, 12 / 64)[position]
        assert actual[0, position] == (second if threshold < ratio else first)


async def test_invalid_image_metadata_and_commands(tmp_path):
    with raises(ProtocolError):
        DrawCommand((1, 26, 0, 0))
    with raises(ProtocolError):
        DrawCommand((0, 4, 3, 0, 0, 0, 0))
    data = ImageData((DrawCommand.brush(1, 4, -999, 0, 0, 0),), 1, 1, 1, 1)
    with raises(ProtocolError):
        data.validate()
    path = tmp_path / 'bad.json'
    path.write_text('{"version":1,"unknown":true}')
    with raises(ProtocolError):
        await ImageData.load(path)


@mark.parametrize(
    'preset,expected',
    [
        ('cluster-dot', 'fd9eef56753a2d4178d67327f496a34ccdc9bf1cba589fabf23bf0777d938c5a'),
        ('yliluoma-1', '3b15c14e390e81cbc48afcc4f66c8327f9609d2cb30ea66763b6d6b9599b86d5'),
    ],
)
async def test_saved_golden_commands(preset, expected):
    # Golden command hashes for this deterministic RGBA fixture.
    source = new('RGBA', (16, 12))
    for y in range(12):
        for x in range(16):
            source.putpixel(
                (x, y), (x * 17, y * 23, (x * 31 + y * 7) % 256, (x * 13 + y * 19) % 256)
            )
    data = await generate_image((await RasterImage.from_pillow(source)), preset=preset)
    wire = dumps([command.values for command in data.commands], separators=(',', ':')).encode()
    assert sha256(wire).hexdigest() == expected
    if preset == 'cluster-dot':
        assert (
            sha256((await render_preview(data)).tobytes()).hexdigest()
            == '2c04fff3d771115f431338575246aac6248fc70c10dcec8d97150340f59f2680'
        )
