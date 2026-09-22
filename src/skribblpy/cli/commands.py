"""Image CLI commands; source images and saved command files share a preview path."""

from asyncio import run
from pathlib import Path
from argly import Option, command
from typing import Optional, Annotated


async def _generate(
    *,
    source: Path,
    output: Path,
    preset: str = 'cluster-dot',
    max_commands: Optional[int] = None,
    max_duration: Optional[float] = None,
) -> int:
    from skribblpy.images import generate_image, drawing_payload_bytes

    data = await generate_image(
        source, preset=preset, max_commands=max_commands, max_duration=max_duration
    )
    await data.save(output)
    print(
        f'{len(data.commands)} commands; estimated playback {data.estimated_send_duration:.2f}s; {await drawing_payload_bytes(data)} event bytes'
    )
    return 0


async def _preview(
    *,
    source: Path,
    output: Path,
    preset: str = 'cluster-dot',
    max_commands: Optional[int] = None,
    max_duration: Optional[float] = None,
) -> int:
    from skribblpy.images import ImageData, generate_image, render_preview

    if source.suffix.lower() == '.json' and (max_commands is not None or max_duration is not None):
        raise ValueError('Drawing budgets apply to source images, not saved drawings')
    data = (
        await ImageData.load(source)
        if source.suffix.lower() == '.json'
        else await generate_image(
            source, preset=preset, max_commands=max_commands, max_duration=max_duration
        )
    )
    image = await render_preview(data)
    await image.save(output, format='PNG')
    return 0


@command('generate', summary='Convert an image to skribbl drawing commands.')
def generate(
    *,
    source: Annotated[Path, Option('--input', '-i')],
    output: Annotated[Path, Option('-o')],
    preset: Annotated[str, Option()] = 'cluster-dot',
    max_commands: Annotated[Optional[int], Option()] = None,
    max_duration: Annotated[Optional[float], Option()] = None,
) -> int:
    return run(
        _generate(
            source=source,
            output=output,
            preset=preset,
            max_commands=max_commands,
            max_duration=max_duration,
        )
    )


@command('preview', summary='Render a source image or drawing JSON to a PNG preview.')
def preview(
    *,
    source: Annotated[Path, Option('--input', '-i')],
    output: Annotated[Path, Option('-o')],
    preset: Annotated[str, Option()] = 'cluster-dot',
    max_commands: Annotated[Optional[int], Option()] = None,
    max_duration: Annotated[Optional[float], Option()] = None,
) -> int:
    return run(
        _preview(
            source=source,
            output=output,
            preset=preset,
            max_commands=max_commands,
            max_duration=max_duration,
        )
    )
