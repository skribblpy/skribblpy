from json import dumps
from subprocess import run
from sys import executable
from asyncio import to_thread
from pytest import mark, raises
from numpy import uint8, frombuffer
from numpy.random import default_rng
from numpy.testing import assert_array_equal
from skribblpy._image_native import plan_strokes
from skribblpy.images.data import MAX_IMAGE_COMMANDS
from skribblpy.images.planning import TONE_CELL_SIZES
from skribblpy.drawing import PALETTE, DrawCommand, DRAW_INTERVAL, DRAW_BATCH_SIZE
from skribblpy.images import (
    ImageData,
    RasterImage,
    generate_image,
    optimize_image,
    render_preview,
    drawing_payload_bytes,
)


@mark.parametrize('color', [0, 1, 4, 14, 25])
async def test_solid_colors_are_exact_with_at_most_one_command(color, tmp_path):
    result = await optimize_image(RasterImage(1, 1, bytes(PALETTE[color])), max_commands=1)
    assert len(result.image.commands) == (color != 0)
    assert (await render_preview(result.image)).pixels == bytes(PALETTE[color]) * 800 * 600
    assert min(candidate.error for candidate in result.candidates) == 0
    path = tmp_path / 'optimized.json'
    await result.image.save(path)
    assert (await ImageData.load(path)) == result.image


@mark.parametrize(
    'commands,duration,expected',
    [
        (None, None, MAX_IMAGE_COMMANDS),
        (None, 0, 8),
        (None, DRAW_INTERVAL, 16),
        (7, 0, 7),
        (20, 0, 8),
        (99, 2 * DRAW_INTERVAL, 24),
        (None, 0.032999, 8),
        (17, DRAW_INTERVAL, 16),
    ],
)
def test_budget_matches_actual_sender_pacing(commands, duration, expected):
    # Check the internal algorithm directly.
    # noinspection PyProtectedMember
    from skribblpy.images.optimize import _budget

    assert _budget(commands, duration) == expected
    if duration is not None:
        assert ((expected - 1) // DRAW_BATCH_SIZE) * DRAW_INTERVAL <= duration


@mark.parametrize(
    'commands,duration',
    [
        (0, None),
        (-1, None),
        (100001, None),
        (True, None),
        (1.5, None),
        (None, -1),
        (None, float('nan')),
        (None, float('inf')),
        (None, True),
    ],
)
async def test_invalid_budget_rejected_before_reading_source(commands, duration):
    with raises(ValueError):
        # Invalid budget types are deliberate test inputs.
        # noinspection PyTypeChecker
        await optimize_image('does-not-exist.png', max_commands=commands, max_duration=duration)


async def test_native_strokes_strictly_reduce_rendered_error_and_are_deterministic():
    source = bytes(PALETTE[4]) * 100 + bytes(PALETTE[14]) * 100
    source *= 150
    values = plan_strokes(source, PALETTE, list(range(26)), 12)
    assert values == plan_strokes(source, PALETTE, list(range(26)), 12)
    target = (
        frombuffer(source, dtype=uint8).reshape(150, 200, 3).repeat(4, 0).repeat(4, 1).astype(float)
    )
    previous = float('inf')
    for end in range(1, len(values) + 1):
        data = ImageData(
            tuple(DrawCommand(tuple(value)) for value in values[:end]), 200, 150, 200, 150
        )
        actual = frombuffer((await render_preview(data)).pixels, dtype=uint8).reshape(600, 800, 3)
        delta = target - actual
        luma = delta @ (0.299, 0.587, 0.114)
        error = float((0.75 * (delta**2 @ (0.299, 0.587, 0.114)) + luma**2).sum())
        assert error < previous
        previous = error


async def test_optimizer_honors_limits_and_preserves_both_regions():
    source = RasterImage(2, 1, bytes(PALETTE[4]) + bytes(PALETTE[14]))
    result = await optimize_image(source, max_commands=64, max_duration=0.3)
    assert len(result.image.commands) <= 64
    assert result.image.estimated_send_duration <= 0.3
    assert all(
        len(candidate.image.commands) <= 64
        for candidate in result.candidates
        if candidate.within_budget
    )
    assert not result.candidates[0].within_budget
    preview = await render_preview(result.image)
    assert preview.getpixel((100, 100)) == PALETTE[4]
    assert preview.getpixel((700, 500)) == PALETTE[14]


@mark.parametrize('count', [0, 1, 8, 9, 16, 17])
async def test_payload_measurement_counts_real_socketio_frames(count):
    commands = tuple(DrawCommand.brush(14, 12, 1, 2, 345, 567) for _ in range(count))
    data = ImageData(commands, 1, 1, 1, 1)
    expected = sum(
        len(
            (
                '42'
                + dumps(
                    [
                        'data',
                        {
                            'id': 19,
                            'data': [command.values for command in commands[start : start + 8]],
                        },
                    ],
                    separators=(',', ':'),
                )
            ).encode()
        )
        for start in range(0, count, 8)
    )
    assert (await drawing_payload_bytes(data)) == expected


async def test_dither_presets_reject_budgets():
    with raises(ValueError, match='require preset=optimized'):
        await generate_image(RasterImage(1, 1, bytes(PALETTE[0])), max_commands=2)


async def test_optimized_cli_budget_and_json_preview(tmp_path):
    source, output = tmp_path / 'source.png', tmp_path / 'drawing.json'
    await RasterImage(1, 1, bytes(PALETTE[4])).save(source)
    result = await to_thread(
        run,
        [
            executable,
            '-m',
            'skribblpy.cli',
            'generate',
            '--input',
            str(source),
            '--output',
            str(output),
            '--preset',
            'optimized',
            '--max-commands',
            '1',
            '--max-duration',
            '0',
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert len((await ImageData.load(output)).commands) == 1
    assert '39 event bytes' in result.stdout


@mark.parametrize('rows,columns', [(75, 100), (50, 66), (15, 21)])
def test_area_means_preserve_fractional_averages_at_uneven_edges(rows, columns):
    # Check the internal algorithm directly.
    # noinspection PyProtectedMember
    from skribblpy.images.planning import _area_means

    pixels = default_rng(7).integers(0, 256, (150, 200, 3), dtype=uint8)
    result = _area_means(pixels, rows, columns)
    for row in range(rows):
        for column in range(columns):
            cell = pixels[
                row * 150 // rows : (row + 1) * 150 // rows,
                column * 200 // columns : (column + 1) * 200 // columns,
            ]
            assert_array_equal(result[row, column], cell.mean(axis=(0, 1)))


def test_cached_tone_plans_are_readonly_across_conversions():
    # Check the internal cached tone plans directly.
    # noinspection PyProtectedMember
    from skribblpy.images.planning import _tone_mixtures

    for size in TONE_CELL_SIZES:
        plans = _tone_mixtures(size)
        assert _tone_mixtures(size) is plans
        for values in plans:
            with raises(ValueError, match='read-only'):
                values.flat[0] = 0
