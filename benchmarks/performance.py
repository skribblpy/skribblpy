"""Run ``python -m benchmarks.performance`` after installing image extras.

Compares indexed matching and vectorized quantization with simple equivalent
Python baselines. Timings are local microbenchmarks, not service throughput.
"""

from json import dumps
from asyncio import run
from random import Random
from statistics import median
from time import perf_counter
from numpy import array, uint8
from skribblpy.state import StateStore
from skribblpy import PALETTE, PacketID
from platform import platform, python_version
from examples.word_guesser.ranking import WordIndex
from skribblpy.images.quantize import cluster_dot, CLUSTER_MATRIX
from skribblpy.images import RasterImage, generate_image, render_preview


def _measure(function, repetitions=5):
    samples = []
    for _ in range(repetitions):
        start = perf_counter()
        function()
        samples.append(perf_counter() - start)
    return median(samples)


async def _measure_async(function, repetitions=5):
    samples = []
    for _ in range(repetitions):
        start = perf_counter()
        await function()
        samples.append(perf_counter() - start)
    return median(samples)


def _scalar_quantize(pixels):
    output = []
    for row, line in enumerate(pixels):
        result = []
        for column, color in enumerate(line):
            threshold = float(CLUSTER_MATRIX[row % 4, column % 4])
            target = tuple(int(value) + threshold for value in color)
            result.append(
                min(
                    range(len(PALETTE)),
                    key=lambda index: sum(
                        (target[channel] - PALETTE[index][channel]) ** 2 for channel in range(3)
                    ),
                )
            )
        output.append(result)
    return array(output, dtype=uint8)


async def main():
    random = Random(314)
    words = {
        ''.join(random.choices('abcdefghijklmnopqrstuvwxyz', k=random.randrange(3, 16)))
        for _ in range(30_000)
    }
    words.add('caterpillar')
    index = WordIndex(words)
    hint = 'c_t________'

    def scan():
        return {
            word
            for word in words
            if len(word) == len(hint)
            and all(mask == '_' or mask == letter for mask, letter in zip(hint, word, strict=True))
        }

    assert {index.words[position] for position in index.matching(hint)} == scan()
    indexed = _measure(lambda: [index.matching(hint) for _ in range(100)]) / 100
    scanned = _measure(lambda: [scan() for _ in range(100)]) / 100
    pixels = array(
        [[[random.randrange(256) for _ in range(3)] for _ in range(200)] for _ in range(150)],
        dtype=uint8,
    )
    assert (cluster_dot(pixels) == _scalar_quantize(pixels)).all()
    scalar = _measure(lambda: _scalar_quantize(pixels), 3)
    vectorized = _measure(lambda: cluster_dot(pixels))
    source = RasterImage(200, 150, pixels.tobytes())
    cluster = await generate_image(source)
    cluster_generation = await _measure_async(lambda: generate_image(source), 3)
    preview = await _measure_async(lambda: render_preview(cluster), 3)
    start = perf_counter()
    yliluoma = await generate_image(source, preset='yliluoma-1')
    yliluoma_generation = perf_counter() - start
    store = StateStore(track_drawing=False)
    count = 100_000
    state_time = _measure(lambda: [store.apply(PacketID.TIME, i % 80) for i in range(count)], 3)
    result = {
        'python': python_version(),
        'platform': platform(),
        'dictionary_words': len(words),
        'matching_indexed_us': indexed * 1e6,
        'matching_scan_us': scanned * 1e6,
        'matching_speedup': scanned / indexed,
        'quantization_vectorized_ms': vectorized * 1000,
        'quantization_scalar_ms': scalar * 1000,
        'quantization_speedup': scalar / vectorized,
        'cluster_generation_ms': cluster_generation * 1000,
        'cluster_commands': len(cluster.commands),
        'preview_ms': preview * 1000,
        'yliluoma_generation_s': yliluoma_generation,
        'yliluoma_commands': len(yliluoma.commands),
        'state_packets_per_second': count / state_time,
    }
    print(dumps(result, indent=2))


if __name__ == '__main__':
    run(main())
