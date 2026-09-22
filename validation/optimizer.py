"""Reproducible drawing budgets, payload measurements, and comparison sheets."""

from json import dumps
from typing import Any
from pathlib import Path
from typing import Annotated
from asyncio import to_thread
from time import perf_counter
from PIL.ImageDraw import Draw
from asyncio import run as run_async
from argly import App, Option, command
from PIL.Image import new, frombytes, Resampling
from skribblpy.images import (
    RasterImage,
    visual_error,
    generate_image,
    optimize_image,
    render_preview,
    drawing_payload_bytes,
)

BUDGETS = (1000, 2000, 4000)
CELL_WIDTH = 400
CELL_HEIGHT = 350


async def compare(source: Path, output: Path):
    # Check the internal algorithm directly.
    # noinspection PyProtectedMember
    from skribblpy.images.generate import _sample

    await to_thread(output.mkdir, parents=True, exist_ok=True)
    width, height, pixels = await to_thread(_sample, source)
    target = RasterImage(800, 600, pixels.repeat(4, 0).repeat(4, 1).tobytes())
    original = await (await RasterImage.load(source)).to_rgb()
    images = [('Source', frombytes('RGB', original.size, original.pixels))]
    report: dict[str, Any] = {'source': str(source), 'source_size': [width, height], 'plans': []}
    for name in ('cluster-dot', 'yliluoma-1', *BUDGETS):
        start = perf_counter()
        if isinstance(name, int):
            result = await optimize_image(source, max_commands=name)
            data = result.image
            selected = result.selected
            candidates = [
                {
                    'name': candidate.name,
                    'commands': len(candidate.image.commands),
                    'event_bytes': candidate.payload_bytes,
                    'error': candidate.error,
                    'within_budget': candidate.within_budget,
                }
                for candidate in result.candidates
            ]
        else:
            data = await generate_image(source, preset=name)
            selected, candidates = name, []
        seconds = perf_counter() - start
        preview = await render_preview(data)
        payload = await drawing_payload_bytes(data)
        record = {
            'name': str(name),
            'selected': selected,
            'commands': len(data.commands),
            'events': (len(data.commands) + 7) // 8,
            'event_bytes': payload,
            'paced_seconds': data.estimated_send_duration,
            'error': (await visual_error(target, preview)),
            'conversion_seconds': seconds,
            'candidates': candidates,
        }
        report['plans'].append(record)
        await data.save(output / f'{source.stem}-{name}.json')
        await preview.save(output / f'{source.stem}-{name}.png')
        label = f'{name}: {selected}\n{len(data.commands):,} commands / {payload:,} bytes / {data.estimated_send_duration:.2f}s'
        images.append((label, frombytes('RGB', preview.size, preview.pixels)))
        print(f'{source.name}: {label}', flush=True)
    sheet = new('RGB', (CELL_WIDTH * 3, CELL_HEIGHT * 2), 'white')
    draw = Draw(sheet)
    for index, (label, picture) in enumerate(images):
        x, y = index % 3 * CELL_WIDTH, index // 3 * CELL_HEIGHT
        sheet.paste(picture.resize((400, 300), Resampling.LANCZOS), (x, y))
        draw.text((x + 6, y + 305), label, fill='black')
    sheet.save(output / f'{source.stem}-comparison.png')
    (output / f'{source.stem}-results.json').write_text(dumps(report, indent=2), encoding='utf-8')
    return report


@command('run', summary='Compare dithering presets and optimized drawing budgets.')
def run(
    *,
    photos: Annotated[Path, Option()],
    output: Annotated[Path, Option()],
) -> int:
    for source in sorted(photos.iterdir()):
        if source.suffix.lower() in ('.png', '.jpg', '.jpeg', '.gif'):
            run_async(compare(source, output))
    return 0


if __name__ == '__main__':
    App.discover('optimizer-validation', __name__).run()
