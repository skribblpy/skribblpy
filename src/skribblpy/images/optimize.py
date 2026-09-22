"""Budgeted drawing plans scored using their rendered pixels and event payloads."""

from pathlib import Path
from math import isfinite
from typing import Optional
from asyncio import to_thread
from dataclasses import dataclass
from skribblpy.images.quantize import _features
from skribblpy.images.raster import RasterImage
from numpy import array, argmin, float64, minimum
from skribblpy.images.render import _render_preview
from skribblpy.images.planning import tone_plans, dither_plans
from skribblpy.images.data import ImageData, MAX_IMAGE_COMMANDS
from skribblpy._image_native import SAMPLE_WIDTH, plan_strokes, SAMPLE_HEIGHT
from skribblpy.images.metrics import _quality_features, _error_from_features, _drawing_payload_bytes
from skribblpy.drawing import (
    PALETTE,
    DrawCommand,
    CANVAS_WIDTH,
    CANVAS_HEIGHT,
    DRAW_INTERVAL,
    DRAW_BATCH_SIZE,
)

PALETTE_SIZES = (6, 12, len(PALETTE))


@dataclass(frozen=True, slots=True)
class PlanCandidate:
    name: str
    image: ImageData
    error: float
    payload_bytes: int
    within_budget: bool


@dataclass(frozen=True, slots=True)
class OptimizationResult:
    image: ImageData
    candidates: tuple[PlanCandidate, ...]
    command_budget: int
    selected: str


def _budget(max_commands: Optional[int], max_duration: Optional[float]) -> int:
    if max_commands is not None and (
        type(max_commands) is not int or not 1 <= max_commands <= MAX_IMAGE_COMMANDS
    ):
        raise ValueError(f'max_commands must be an integer from 1 to {MAX_IMAGE_COMMANDS}')
    if max_duration is not None and (
        isinstance(max_duration, bool)
        or not isinstance(max_duration, (int, float))
        or not isfinite(max_duration)
        or max_duration < 0
    ):
        raise ValueError('max_duration must be a finite nonnegative number of seconds')
    limit = max_commands if max_commands is not None else MAX_IMAGE_COMMANDS
    if max_duration is not None:
        # Use the sender's exact duration expression, avoiding float floor boundary errors.
        low, high = 1, MAX_IMAGE_COMMANDS
        while low < high:
            middle = (low + high + 1) // 2
            if ((middle - 1) // DRAW_BATCH_SIZE) * DRAW_INTERVAL <= max_duration:
                low = middle
            else:
                high = middle - 1
        limit = min(limit, low)
    return limit


def _palettes(pixels):
    targets = _features(pixels.reshape(-1, 3).astype(float64))
    colors = _features(array(PALETTE, dtype=float64))
    distances = ((targets[:, None] - colors) ** 2).sum(axis=-1)
    chosen: list[int] = []
    best = distances[:, 0] * 0 + float('inf')
    for _ in range(max(PALETTE_SIZES)):
        costs = minimum(best[:, None], distances).sum(axis=0)
        costs[chosen] = float('inf')
        color = int(argmin(costs))
        chosen.append(color)
        best = minimum(best, distances[:, color])
        if len(chosen) in PALETTE_SIZES:
            yield tuple(chosen)


def _optimize_image(
    source: RasterImage | str | Path,
    *,
    max_commands: Optional[int] = None,
    max_duration: Optional[float] = None,
) -> OptimizationResult:
    from skribblpy.images.generate import _runs, _sample, _run_count
    from skribblpy.images.quantize import yliluoma, cluster_dot

    budget = _budget(max_commands, max_duration)
    width, height, pixels = _sample(source)
    target_pixels = pixels.repeat(CANVAS_HEIGHT // SAMPLE_HEIGHT, axis=0).repeat(
        CANVAS_WIDTH // SAMPLE_WIDTH, axis=1
    )
    target = RasterImage(CANVAS_WIDTH, CANVAS_HEIGHT, target_pixels.tobytes())
    target_features = _quality_features(target)
    candidates: list[PlanCandidate] = []

    def evaluate(plan_name: str, plan_commands: tuple[DrawCommand, ...], preset: str):
        data = ImageData(plan_commands, width, height, width, height, preset=preset)
        candidates.append(
            PlanCandidate(
                plan_name,
                data,
                _error_from_features(target_features, _render_preview(data)),
                _drawing_payload_bytes(data),
                len(plan_commands) <= budget,
            )
        )

    grid = cluster_dot(pixels)
    vertical = _run_count(grid, vertical=True) < _run_count(grid, vertical=False)
    evaluate('cluster-dot', _runs(grid, vertical=vertical), 'cluster-dot')
    if candidates[0].error > 0:
        grid = yliluoma(pixels)
        vertical = _run_count(grid, vertical=True) < _run_count(grid, vertical=False)
        evaluate('yliluoma-1', _runs(grid, vertical=vertical), 'yliluoma-1')
    for colors in _palettes(pixels):
        commands = tuple(
            DrawCommand(tuple(values))
            for values in plan_strokes(pixels.tobytes(), PALETTE, colors, budget)
        )
        evaluate(f'regions-{len(colors)}', commands, 'optimized')
    if min(candidate.error for candidate in candidates if candidate.within_budget) > 0:
        for name, commands in dither_plans(pixels, budget):
            evaluate(name, commands, 'optimized')
        for name, commands in tone_plans(pixels, budget):
            evaluate(name, commands, 'optimized')

    def score(candidate: PlanCandidate) -> tuple[float, int, int]:
        return candidate.error, candidate.payload_bytes, len(candidate.image.commands)

    eligible: list[PlanCandidate] = [item for item in candidates if item.within_budget]
    winner: PlanCandidate = min(eligible, key=score)
    return OptimizationResult(winner.image, tuple(candidates), budget, winner.name)


async def optimize_image(
    source: RasterImage | str | Path,
    *,
    max_commands: Optional[int] = None,
    max_duration: Optional[float] = None,
) -> OptimizationResult:
    """Compare rendered plans off the event loop and return the best eligible plan.

    Both limits default to None (unlimited within the protocol command limit).
    Duration counts pacing delays, not network latency. Playback starts white.
    Cancellation stops waiting; an already running worker may finish.
    """
    return await to_thread(
        _optimize_image,
        source,
        max_commands=max_commands,
        max_duration=max_duration,
    )
