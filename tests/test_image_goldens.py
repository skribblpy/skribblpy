from pathlib import Path
from numpy import asarray
from hashlib import sha256
from json import dumps, loads
from pytest import mark, fixture
from PIL.Image import frombytes, open as open_image
from skribblpy.images import RasterImage, generate_image, render_preview
from validation.image_cases import drawing_cases, conversion_cases, save_animated_fixture

GOLDENS = loads(Path(__file__).with_name('fixtures').joinpath('image_goldens.json').read_text())


@fixture(scope='module')
def sources(tmp_path_factory):
    folder = tmp_path_factory.mktemp('image-goldens')
    result = {}
    for name, source in conversion_cases():
        path = folder / f'{name}.png'
        source.save(path)
        result[name] = path
    path = folder / 'animated-transparent.gif'
    save_animated_fixture(path)
    result['animated-transparent'] = path
    return result


@mark.parametrize('name,preset,expected', GOLDENS['conversion'])
async def test_conversion_and_preview_match_goldens(name, preset, expected, sources):
    data = await generate_image(sources[name], preset=preset)
    wire = dumps([command.values for command in data.commands], separators=(',', ':')).encode()
    assert sha256(wire).hexdigest() == expected['commands']
    assert sha256((await render_preview(data)).tobytes()).hexdigest() == expected['preview']


@mark.parametrize('name,data', list(drawing_cases()))
async def test_difficult_drawing_replay_matches_goldens(name, data):
    assert sha256((await render_preview(data)).tobytes()).hexdigest() == GOLDENS['drawing'][name]


@mark.parametrize('preset', ['cluster-dot', 'yliluoma-1'])
async def test_animated_gif_uses_first_frame(preset, sources):
    path = sources['animated-transparent']
    with open_image(path) as image:
        first = await generate_image((await RasterImage.from_pillow(image.copy())), preset=preset)
        image.seek(1)
        second = await generate_image((await RasterImage.from_pillow(image.copy())), preset=preset)
    assert (await generate_image(path, preset=preset)) == first
    assert first.commands != second.commands


@mark.parametrize('mode', ['I;16', 'I;16L', 'I;16B', 'I;16N'])
async def test_gray16_byte_orders_preserve_dynamic_range(mode, sources):
    with open_image(sources['gray16-ramp']) as image:
        dtype = '>u2' if mode == 'I;16B' else '=u2' if mode == 'I;16N' else '<u2'
        pixels = asarray(image).astype(dtype)
        data = await generate_image(
            await RasterImage.from_pillow(frombytes(mode, image.size, pixels.tobytes()))
        )
    preview = await render_preview(data)
    assert preview.getpixel((0, 300)) == (0, 0, 0)
    assert preview.getpixel((799, 300)) == (255, 255, 255)
    expected = next(
        item
        for name, preset, item in GOLDENS['conversion']
        if name == 'gray16-ramp' and preset == 'cluster-dot'
    )
    assert sha256(preview.tobytes()).hexdigest() == expected['preview']
