from pathlib import Path
from subprocess import run
from sys import executable
from asyncio import to_thread
from skribblpy.images import ImageData
from PIL.Image import new, open as open_image


async def test_argly_image_commands(tmp_path):
    source, drawing, preview = (
        tmp_path / name for name in ('source.png', 'drawing.json', 'preview.png')
    )
    new('RGB', (2, 2), (239, 19, 11)).save(source)
    generated = await to_thread(
        run,
        [
            executable,
            '-m',
            'skribblpy.cli',
            'generate',
            '--input',
            str(source),
            '--output',
            str(drawing),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert generated.returncode == 0, generated.stderr
    assert (await ImageData.load(drawing)).commands
    rendered = await to_thread(
        run,
        [
            executable,
            '-m',
            'skribblpy.cli',
            'preview',
            '--input',
            str(drawing),
            '--output',
            str(preview),
        ],
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert rendered.returncode == 0, rendered.stderr
    with open_image(preview) as image:
        assert image.size == (800, 600)


def test_guesser_cli_help_and_missing_database(tmp_path):
    root = Path(__file__).resolve().parents[1]
    help_result = run(
        [executable, '-m', 'examples.word_guesser', 'run', '--help'],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert help_result.returncode == 0
    assert '--database' in help_result.stdout and '--max-clients' in help_result.stdout
    missing = run(
        [
            executable,
            '-m',
            'examples.word_guesser',
            'run',
            '--database',
            str(tmp_path / 'missing.json'),
        ],
        cwd=root,
        capture_output=True,
        text=True,
        timeout=15,
    )
    assert missing.returncode != 0
