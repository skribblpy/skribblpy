"""Measure complete image optimization and fingerprint every candidate's output."""

from json import dumps
from pathlib import Path
from hashlib import sha256
from cProfile import Profile
from typing import Annotated
from statistics import median
from time import perf_counter
from platform import python_version
from asyncio import run as run_async
from argly import App, Option, command
from skribblpy.images import optimize_image, render_preview


async def _measure(source, budget, repetitions):
    samples = []
    fingerprints = []
    for _ in range(repetitions):
        started = perf_counter()
        result = await optimize_image(source, max_commands=budget)
        samples.append(perf_counter() - started)
        candidates = []
        for candidate in result.candidates:
            preview = await render_preview(candidate.image)
            candidates.append(
                {
                    'name': candidate.name,
                    'commands': len(candidate.image.commands),
                    'payload_bytes': candidate.payload_bytes,
                    'error': candidate.error,
                    'eligible': candidate.within_budget,
                    'commands_sha256': sha256(
                        dumps(
                            [item.values for item in candidate.image.commands],
                            separators=(',', ':'),
                        ).encode()
                    ).hexdigest(),
                    'preview_sha256': sha256(preview.pixels).hexdigest(),
                }
            )
        fingerprints.append({'selected': result.selected, 'candidates': candidates})
    assert all(value == fingerprints[0] for value in fingerprints)
    return {'seconds': samples, 'median_seconds': median(samples), **fingerprints[0]}


@command('run', summary='Benchmark budgeted and unlimited conversion with output checks.')
def benchmark(
    *,
    source: Annotated[Path, Option()],
    output: Annotated[Path, Option()],
    repetitions: Annotated[int, Option()] = 3,
    profile: Annotated[str, Option()] = '',
) -> int:
    if repetitions < 1:
        raise ValueError('Repetitions must be positive')
    results = {}
    for budget in (2000, None):
        results[str(budget)] = run_async(_measure(source, budget, repetitions))
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        dumps(
            {
                'python': python_version(),
                'source_sha256': sha256(source.read_bytes()).hexdigest(),
                'results': results,
            },
            indent=2,
        ),
        encoding='utf-8',
    )
    if profile:
        # Check the internal algorithm directly.
        # noinspection PyProtectedMember
        from skribblpy.images.optimize import _optimize_image

        with Profile() as profiler:
            _optimize_image(source, max_commands=2000)
        profiler.dump_stats(profile)
    print(dumps({key: value['median_seconds'] for key, value in results.items()}))
    return 0


if __name__ == '__main__':
    raise SystemExit(App.discover('skribbl-optimizer-benchmark', 'benchmarks.optimizer').run())
