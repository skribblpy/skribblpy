"""Check distributions and test installed wheels outside the source checkout."""

from json import dumps
from os import environ
from pathlib import Path
from tomllib import loads
from hashlib import sha256
from subprocess import run
from sys import executable
from venv import EnvBuilder
from zipfile import ZipFile
from typing import Annotated
from os import name as os_name
from email.parser import BytesParser
from tarfile import open as open_tar
from tempfile import TemporaryDirectory
from argly import App, Flag, Option, command
from platform import platform, python_version

_CORE_SMOKE = """
import sys
from pathlib import Path
from asyncio import run
from importlib.util import find_spec
from importlib.metadata import version
import skribblpy
from skribblpy import Event, Client, Snapshot, EventName
assert Path(skribblpy.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
assert version('skribblpy') == sys.argv[1]
assert all(find_spec(name) is None for name in ('numpy', 'argly', 'PIL'))
assert 'skribblpy._image_native' not in sys.modules
async def main():
    client = Client()
    events = []
    @client.on(EventName.CHAT)
    async def receive(event):
        events.append(event)
    await client.dispatch(Event(EventName.CHAT, Snapshot(), 1, 0))
    assert len(events) == 1
    await client.close()
run(main())
print('Core installation and async callbacks passed')
"""

_IMAGE_SMOKE = """
import sys
from pathlib import Path
from asyncio import run
from importlib.util import find_spec
from skribblpy import PALETTE, _image_native
from skribblpy.images import ImageData, RasterImage, generate_image, render_preview
assert find_spec('PIL') is None
assert Path(_image_native.__file__).resolve().is_relative_to(Path(sys.prefix).resolve())
async def main():
    source = Path('source.png')
    await RasterImage(1, 1, bytes(PALETTE[4])).save(source)
    for preset in ('cluster-dot', 'yliluoma-1', 'optimized'):
        data = await generate_image(source, preset=preset)
        await data.save('drawing.json')
        assert await ImageData.load('drawing.json') == data
        preview = await render_preview(data)
        assert preview.getpixel((400, 300)) == PALETTE[4]
        assert await RasterImage.from_encoded(await preview.to_png()) == preview
run(main())
print('Image conversion, rendering, and file round trips passed without Pillow')
"""


def _run(arguments, *, cwd):
    # Do not let an editable checkout leak into the isolated environment.
    environment = {
        key: value for key, value in environ.items() if key not in ('PYTHONPATH', 'PYTHONHOME')
    }
    run([str(value) for value in arguments], cwd=cwd, env=environment, check=True, timeout=600)


def _python(environment):
    return environment / ('Scripts/python.exe' if os_name == 'nt' else 'bin/python')


def _install(python, wheel, extra, cwd):
    requirement = f'skribblpy[{extra}] @ {wheel.as_uri()}' if extra else str(wheel)
    print(f'Checking installation: {extra or "core"}', flush=True)
    _run(
        [python, '-m', 'pip', 'install', '--quiet', '--disable-pip-version-check', requirement],
        cwd=cwd,
    )
    _run([python, '-m', 'pip', 'check'], cwd=cwd)


def _archives(wheel, sdist, source, destination):
    project = loads((source / 'pyproject.toml').read_text(encoding='utf-8'))['project']
    version = project['version']
    native = loads((source / 'native/Cargo.toml').read_text(encoding='utf-8'))['package']
    if native['version'] != version:
        raise ValueError('Python and Rust package versions differ')
    tag = environ.get('RELEASE_TAG') or (
        environ.get('GITHUB_REF_NAME') if environ.get('GITHUB_REF_TYPE') == 'tag' else ''
    )
    if tag and tag != f'v{version}':
        raise ValueError('Release tag must match the package version')
    with ZipFile(wheel) as archive:
        names = set(archive.namelist())
        required = {
            'skribblpy/__init__.py',
            'skribblpy/py.typed',
            'skribblpy/_image_native.pyi',
            'skribblpy/_native_licenses.json',
            'skribblpy/cli/__main__.py',
            'skribblpy/images/optimize.py',
        }
        if not required <= names:
            raise ValueError(f'Wheel is missing {required - names}')
        if any(name.endswith(('.pdb', '.pyc')) or '__pycache__' in name for name in names):
            raise ValueError('Wheel contains debug or cache files')
        if not any(
            name.startswith('skribblpy/_image_native') and name.endswith(('.pyd', '.so'))
            for name in names
        ):
            raise ValueError('Wheel is missing the native extension')
        if not any(name.endswith('.dist-info/licenses/LICENSE') for name in names):
            raise ValueError('Wheel is missing the MIT license')
        metadata_name = next(name for name in names if name.endswith('.dist-info/METADATA'))
        metadata = BytesParser().parsebytes(archive.read(metadata_name))
        if metadata['Name'] != project['name'] or metadata['Version'] != version:
            raise ValueError('Wheel metadata does not match the source')
        for path in (source / 'src/skribblpy').rglob('*'):
            if path.is_file() and (
                path.suffix in ('.py', '.pyi', '.json') or path.name == 'py.typed'
            ):
                name = path.relative_to(source / 'src').as_posix()
                if name not in names or archive.read(name) != path.read_bytes():
                    raise ValueError(f'Wheel does not contain the current source: {name}')
    with open_tar(sdist, 'r:gz') as archive:
        archive.extractall(destination, filter='data')
    roots = list(destination.iterdir())
    if len(roots) != 1 or not roots[0].is_dir():
        raise ValueError('Expected one source distribution root')
    unpacked = roots[0]
    for name in (
        'pyproject.toml',
        'README.md',
        'LICENSE',
        'rust-toolchain.toml',
        'native/Cargo.lock',
        'native/Cargo.toml',
        'native/src/lib.rs',
        'src/skribblpy/py.typed',
        'src/skribblpy/_native_licenses.json',
        'tests/fixtures/image_goldens.json',
        'tests/fixtures/animated-transparent.gif',
        'examples/word_guesser/words.json',
        'examples/word_guesser/words.stats.json',
        'validation/image_cases.py',
    ):
        if not (unpacked / name).is_file():
            raise ValueError(f'Source distribution is missing {name}')
    if any(path.suffix in ('.pdb', '.pyd', '.so', '.pyc') for path in unpacked.rglob('*')):
        raise ValueError('Source distribution contains compiled or debug files')
    return version, unpacked


@command('check', summary='Validate distributions and test isolated installations.')
def check(
    *,
    artifacts: Annotated[Path, Option()],
    work_directory: Annotated[Path, Option()],
    full_suite: Annotated[bool, Flag()] = False,
) -> int:
    artifacts = artifacts.resolve()
    work_directory = work_directory.resolve()
    source = Path(__file__).resolve().parents[1]
    wheels, sdists = list(artifacts.glob('*.whl')), list(artifacts.glob('*.tar.gz'))
    if len(wheels) != 1 or len(sdists) != 1:
        raise ValueError('Provide exactly one wheel and one source distribution per validation')
    wheel, sdist = wheels[0], sdists[0]
    work_directory.mkdir(parents=True, exist_ok=True)
    _run([executable, '-m', 'twine', 'check', '--strict', wheel, sdist], cwd=work_directory)
    with TemporaryDirectory(prefix='skribbl-release-', dir=work_directory) as temporary:
        folder = Path(temporary)
        unpacked = folder / 'sdist'
        unpacked.mkdir()
        version, suite = _archives(wheel, sdist, source, unpacked)
        smoke = folder / 'smoke'
        smoke.mkdir()
        core, cli = folder / 'core', folder / 'cli'
        EnvBuilder(with_pip=True).create(core)
        core_python = _python(core)
        _install(core_python, wheel, '', smoke)
        _run([core_python, '-I', '-c', _CORE_SMOKE, version], cwd=smoke)
        _install(core_python, wheel, 'images', smoke)
        _run([core_python, '-I', '-c', _IMAGE_SMOKE], cwd=smoke)
        EnvBuilder(with_pip=True).create(cli)
        cli_python = _python(cli)
        _install(cli_python, wheel, 'cli', smoke)
        _run([cli_python, '-I', '-c', _IMAGE_SMOKE], cwd=smoke)
        entrypoint = cli_python.with_name(
            'skribbl-image.exe' if cli_python.suffix == '.exe' else 'skribbl-image'
        )
        _run([entrypoint, '--help'], cwd=smoke)
        _run(
            [
                entrypoint,
                'generate',
                '--input',
                'source.png',
                '--output',
                'cli.json',
                '--preset',
                'optimized',
                '--max-commands',
                '1',
            ],
            cwd=smoke,
        )
        _run(
            [
                cli_python,
                '-I',
                '-m',
                'skribblpy.cli',
                'preview',
                '--input',
                'cli.json',
                '--output',
                'cli.png',
            ],
            cwd=smoke,
        )
        if full_suite:
            _install(cli_python, wheel, 'dev', smoke)
            # Run the sdist's tests against the installed wheel, never src/.
            _run(
                [
                    cli_python,
                    '-m',
                    'pytest',
                    '-q',
                    'tests',
                    '--import-mode=importlib',
                    '--cov=skribblpy',
                    '--cov-branch',
                    '--cov-report=term-missing:skip-covered',
                    f'--cov-report=xml:{work_directory / "coverage.xml"}',
                ],
                cwd=suite,
            )
    report = {
        'version': version,
        'python': python_version(),
        'platform': platform(),
        'commit': environ.get('GITHUB_SHA'),
        'core': 'passed',
        'images': 'passed',
        'cli': 'passed',
        'full_suite': full_suite,
        'artifacts': {path.name: sha256(path.read_bytes()).hexdigest() for path in (wheel, sdist)},
    }
    (work_directory / 'validation.json').write_text(
        dumps(report, indent=2) + '\n', encoding='utf-8'
    )
    print(dumps(report, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(App.discover('skribbl-release', 'validation.release').run())
