"""Profile the supplied dictionary and representative image workloads."""

from pathlib import Path
from hashlib import sha256
from cProfile import Profile
from typing import Annotated
from json import dumps, loads
from statistics import median
from time import perf_counter
from asyncio import run as run_async
from argly import App, Flag, Option, command
from collections.abc import Callable, Awaitable
from examples.word_guesser.ranking import WordIndex

DEFAULT_DATABASE = Path(__file__).resolve().parents[1] / 'examples/word_guesser/words.json'


def _measure[T](function: Callable[[], T], repetitions: int = 7) -> tuple[float, T]:
    if repetitions < 1:
        raise ValueError('At least one repetition is required')
    started = perf_counter()
    result = function()
    durations = [perf_counter() - started]
    for _ in range(1, repetitions):
        started = perf_counter()
        result = function()
        durations.append(perf_counter() - started)
    return median(durations), result


async def _measure_async[T](
    function: Callable[[], Awaitable[T]], repetitions: int = 7
) -> tuple[float, T]:
    if repetitions < 1:
        raise ValueError('At least one repetition is required')
    started = perf_counter()
    result = await function()
    durations = [perf_counter() - started]
    for _ in range(1, repetitions):
        started = perf_counter()
        result = await function()
        durations.append(perf_counter() - started)
    return median(durations), result


async def _images():
    from PIL.Image import new, fromarray
    from numpy import indices, uint8, stack
    from skribblpy.images import RasterImage, generate_image, render_preview

    y, x = indices((600, 800))
    gradient = stack((x * 255 // 799, y * 255 // 599, (x + y) * 255 // 1398), axis=-1).astype(uint8)
    poster = new('RGB', (800, 600), 'white')
    poster.paste((239, 19, 11), (40, 40, 760, 240))
    poster.paste((0, 178, 255), (40, 300, 360, 560))
    poster.paste((0, 70, 25), (440, 300, 760, 560))
    textured = ((x * 37 + y * 19 + (x * y) % 97) % 256).astype(uint8)
    texture = fromarray(stack((textured, textured ^ 85, textured ^ 170), axis=-1))
    results = {}
    for name, image in (
        ('poster', poster),
        ('gradient', fromarray(gradient)),
        ('texture', texture),
    ):
        image = await RasterImage.from_pillow(image)
        for preset in ('cluster-dot', 'yliluoma-1'):
            duration, data = await _measure_async(
                lambda source=image, mode=preset: generate_image(source, preset=mode), 3
            )
            render_time, preview = await _measure_async(
                lambda drawing=data: render_preview(drawing), 3
            )
            results[f'{name}/{preset}'] = {
                'generation_ms': duration * 1000,
                'preview_ms': render_time * 1000,
                'commands': len(data.commands),
                'preview_sha256': sha256(preview.tobytes()).hexdigest(),
                'commands_sha256': sha256(
                    dumps(
                        [stroke.values for stroke in data.commands], separators=(',', ':')
                    ).encode()
                ).hexdigest(),
            }
    return results


def workload(database: Path, *, images: bool = False):
    statistics = database.with_name(database.stem + '.stats.json')
    records = loads(database.read_text(encoding='utf-8'))
    counts = loads(statistics.read_text(encoding='utf-8'))['words']
    build_time, index = _measure(
        lambda: WordIndex([record['word'] for record in records], counts), 3
    )
    cases = {}
    for size in (3, 4, 5, 6, 7, 8, 9, 10):
        cases[f'length_{size}'] = ('_' * size, ())
    for word in ('apple', 'banana', 'ice cream', 'Spider-Man', 'New York', 'skateboard'):
        hint = ''.join(
            char if position == 0 or not char.isalpha() else '_'
            for position, char in enumerate(word)
        )
        cases[word] = (hint, (word[:-1],))
    results = {}
    for name, (hint, close) in cases.items():
        duration, candidates = _measure(
            lambda pattern=hint, guesses=close: index.candidates(pattern, close_guesses=guesses)
        )
        matching, _ = _measure(lambda pattern=hint: index.matching(pattern))
        results[name] = {
            'rank_ms': duration * 1000,
            'match_us': matching * 1e6,
            'candidate_count': len(candidates),
            'ranking_sha256': sha256(dumps(candidates).encode()).hexdigest(),
        }
    result = {
        'database_sha256': sha256(database.read_bytes()).hexdigest(),
        'statistics_sha256': sha256(statistics.read_bytes()).hexdigest(),
        'word_count': len(index.words),
        'observations': sum(counts.values()),
        'index_build_ms': build_time * 1000,
        'ranking': results,
    }
    if images:
        result['images'] = run_async(_images())
    return result


@command('run', summary='Profile the actual guesser database and optional image workloads.')
def benchmark(
    *,
    database: Annotated[Path, Option()] = DEFAULT_DATABASE,
    images: Annotated[bool, Flag()] = False,
    profile: Annotated[str, Option()] = '',
) -> int:
    profiler = Profile() if profile else None
    if profiler:
        profiler.enable()
    result = workload(database, images=images)
    if profiler:
        profiler.disable()
        profiler.dump_stats(profile)
    print(dumps(result, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(App.discover('skribbl-benchmark', 'benchmarks.workload').run())
