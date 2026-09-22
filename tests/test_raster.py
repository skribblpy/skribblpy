from struct import pack
from subprocess import run
from sys import executable
from pytest import mark, raises
from zlib import crc32, compress
from skribblpy import PALETTE, ImageError
from dataclasses import FrozenInstanceError
from skribblpy import _image_native as native
from skribblpy.images import RasterImage, generate_image


def _png_chunk(name, payload):
    return pack('>I', len(payload)) + name + payload + pack('>I', crc32(name + payload))


def _png(width, height, depth, kind, scanlines):
    return (
        b'\x89PNG\r\n\x1a\n'
        + _png_chunk(b'IHDR', pack('>IIBBBBB', width, height, depth, kind, 0, 0, 0))
        + _png_chunk(b'IDAT', compress(scanlines))
        + _png_chunk(b'IEND', b'')
    )


@mark.parametrize(
    'mode,channels,kind', [('I;16', 1, 0), ('LA;16', 2, 4), ('RGB;16', 3, 2), ('RGBA;16', 4, 6)]
)
async def test_sixteen_bit_png_preserves_raw_precision(mode, channels, kind, tmp_path):
    values = (12345, 23456, 34567, 45678)[:channels]
    png = _png(1, 1, 16, kind, b'\0' + pack('>' + 'H' * channels, *values))
    expected = RasterImage(1, 1, pack('<' + 'H' * channels, *values), mode)
    path = tmp_path / 'pixel.data'
    path.write_bytes(png)
    assert (await RasterImage.from_encoded(png)) == (await RasterImage.load(path)) == expected
    assert (await generate_image(path)) == (await generate_image(expected))
    assert (await RasterImage.from_encoded((await expected.to_png()))) == (await expected.to_rgb())


async def test_buffer_api_is_immutable_and_exports_png(tmp_path):
    source = RasterImage(2, 1, bytes((239, 19, 11, 0, 0, 0)))
    assert (await source.to_rgb()) is source
    assert source.getpixel((1, 0)) == (0, 0, 0)
    assert source.size == (2, 1) and source.tobytes() == source.pixels
    with raises(FrozenInstanceError):
        # Frozen fields must also reject writes at runtime.
        # noinspection PyDataclass
        source.width = 4
    path = tmp_path / '预览.png'
    await source.save(path)
    assert (await RasterImage.load(path)) == source
    with raises(ImageError, match='PNG'):
        await source.save(tmp_path / 'bad.jpg', format='JPEG')
    with raises(IndexError):
        source.getpixel((-1, 0))


@mark.parametrize(
    'mode,pixels,expected',
    [
        ('L', b'\x80', (128, 128, 128)),
        ('LA', b'\x00\x80', (127, 127, 127)),
        ('RGBA', bytes((1, 2, 3, 0)), (255, 255, 255)),
    ],
)
async def test_compositing_and_png_export(mode, pixels, expected):
    source = RasterImage(1, 1, pixels, mode)
    assert (await source.to_rgb()).getpixel((0, 0)) == expected
    assert (await RasterImage.from_encoded((await source.to_png()))).getpixel((0, 0)) == expected
    with raises(ImageError):
        source.getpixel((0, 0))


@mark.parametrize(
    'arguments',
    [
        (0, 1, b''),
        (-1, 1, b''),
        (True, 1, b'\0\0\0'),
        (1, 1, b''),
        (100001, 1000, b''),
        (1, 1, b'\0', 'invalid'),
    ],
)
def test_invalid_buffers_are_rejected(arguments: tuple):
    with raises(ImageError):
        RasterImage(*arguments)
    with raises(TypeError):
        RasterImage(1, 1, bytearray(3))


async def test_decode_errors_and_limits(tmp_path):
    missing = tmp_path / 'missing.png'
    with raises(FileNotFoundError):
        await RasterImage.load(missing)
    with raises(FileNotFoundError):
        await generate_image(missing)
    with raises(ImageError):
        await RasterImage.from_encoded(b'not an image')
    with raises(ImageError, match='dimensions'):
        await RasterImage.from_encoded(_png(100001, 1000, 8, 2, b''))
    invalid = tmp_path / 'invalid.png'
    invalid.write_bytes(b'not an image')
    with raises(ImageError):
        await generate_image(invalid)


@mark.parametrize(
    'values',
    [
        (1, 26, 0, 0),
        (1, 1, -1, 0),
        (0, 1, 99, 0, 0, 1, 1),
        (0, 1, 4, -10000, 0, 1, 1),
        (9, 1, 0, 0),
    ],
)
def test_native_boundary_validates_commands(values: tuple[int, ...]):
    with raises(ValueError):
        native.render([values], PALETTE)


def test_native_limits_match_python_models():
    from skribblpy.images.data import MAX_SOURCE_PIXELS
    from skribblpy.images.generate import SAMPLE_WIDTH, SAMPLE_HEIGHT

    assert MAX_SOURCE_PIXELS == native.MAX_SOURCE_PIXELS
    assert (SAMPLE_WIDTH, SAMPLE_HEIGHT) == (200, 150)
    assert len(native.render([], PALETTE)) == 800 * 600 * 3


def test_runtime_and_cli_do_not_import_pillow(tmp_path):
    script = """
import sys
from pathlib import Path
from asyncio import run
class BlockPillow:
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'PIL' or fullname.startswith('PIL.'):
            raise AssertionError('Pillow was imported at runtime')
sys.meta_path.insert(0, BlockPillow())
from skribblpy.images import RasterImage, generate_image, render_preview
from skribblpy.cli.commands import generate, preview
folder = Path(sys.argv[1])
source = folder / 'source.png'
async def main():
    await RasterImage(1, 1, bytes((239,19,11))).save(source)
    for preset in ('cluster-dot','yliluoma-1','optimized'):
        data = await generate_image(source, preset=preset)
        await data.save(folder / 'drawing.json')
        image = await render_preview(data)
        assert image.getpixel((20,20)) == (239,19,11)
        assert await RasterImage.from_encoded(await image.to_png()) == image
run(main())
assert generate(source=source, output=folder / 'cli.json') == 0
assert preview(source=folder / 'cli.json', output=folder / 'cli.png') == 0
assert not any(name == 'PIL' or name.startswith('PIL.') for name in sys.modules)
"""
    result = run(
        [executable, '-c', script, str(tmp_path)], capture_output=True, text=True, timeout=20
    )
    assert result.returncode == 0, result.stderr


def test_core_does_not_load_image_dependencies():
    script = """
import sys
class BlockImages:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ('numpy','PIL','skribblpy._image_native'):
            raise AssertionError('Core loaded an image dependency')
sys.meta_path.insert(0, BlockImages())
from skribblpy import Client
assert Client
"""
    result = run([executable, '-c', script], capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
