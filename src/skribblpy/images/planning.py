"""Coarse dither and two-color stripe candidates for budgeted conversion."""

from functools import lru_cache
from skribblpy.images.quantize import yliluoma, _features
from skribblpy.constants import MAX_BRUSH_DIAMETER, MIN_BRUSH_DIAMETER
from numpy import array, int64, uint8, zeros, arange, float64, bincount
from skribblpy.drawing import PALETTE, DrawCommand, CANVAS_WIDTH, CANVAS_HEIGHT

DITHER_CELL_SIZES = (8, 10, 16, 20, 25, 38)
TONE_CELL_SIZES = (8, 10, 12, 16, 20, 25, 38)
MIX_CONTRAST_PENALTY = 0.1
MIXING_BATCH_SIZE = 128


def _area_means(pixels, rows, columns):
    # Integer prefix sums preserve exact cell sums, including uneven edge cells.
    height, width = pixels.shape[:2]
    prefix = zeros((height + 1, width + 1, 3), dtype=int64)
    prefix[1:, 1:] = pixels.cumsum(axis=0, dtype=int64).cumsum(axis=1)
    y = arange(rows + 1) * height // rows
    x = arange(columns + 1) * width // columns
    corners = prefix[y[:, None], x]
    totals = corners[1:, 1:] - corners[:-1, 1:] - corners[1:, :-1] + corners[:-1, :-1]
    counts = (y[1:] - y[:-1])[:, None] * (x[1:] - x[:-1])
    return totals / counts[..., None]


@lru_cache(maxsize=len(TONE_CELL_SIZES))
def _tone_mixtures(size):
    palette = array(PALETTE, dtype=float64)
    features = _features(palette)
    plans, selections, penalties = [], [], []
    for first in range(len(PALETTE)):
        plans.append(palette[first])
        selections.append((first, first, 0))
        penalties.append(0.0)
        for diameter in range(MIN_BRUSH_DIAMETER, size // 2 + 3):
            extent = diameter // 2
            length = size - 2 * extent
            area = sum(
                length
                + 1
                + 2 * max(x for x in range(extent + 1) if 4 * (x * x + y * y) < diameter * diameter)
                for y in range(-extent, diameter - extent)
                if 4 * y * y < diameter * diameter
            )
            ratio = area / (size * size)
            if ratio > 0.5:
                continue
            for second in range(len(PALETTE)):
                if second == first:
                    continue
                plans.append(palette[first] * (1 - ratio) + palette[second] * ratio)
                selections.append((first, second, diameter))
                penalties.append(
                    MIX_CONTRAST_PENALTY * float(((features[first] - features[second]) ** 2).sum())
                )
    plan_features = _features(array(plans))
    costs = (plan_features**2).sum(axis=-1) + array(penalties)
    selection_array = array(selections)
    for values in (plan_features, costs, selection_array):
        values.flags.writeable = False
    return plan_features, costs, selection_array


def dither_plans(pixels, budget):
    for size in DITHER_CELL_SIZES:
        columns = (CANVAS_WIDTH + size - 1) // size
        rows = (CANVAS_HEIGHT + size - 1) // size
        reduced = _area_means(pixels, rows, columns).astype(uint8)
        grid = yliluoma(reduced)
        background = int(bincount(grid.ravel(), minlength=len(PALETTE)).argmax())
        choices = []
        for vertical in (False, True):
            oriented = grid.T if vertical else grid
            commands = [] if background == 0 else [DrawCommand.fill(background)]
            # Use overlapping rows to cover the rounded brush corners.
            pitch = max(1, size * 3 // 4)
            across = CANVAS_HEIGHT if vertical else CANVAS_WIDTH
            along = CANVAS_WIDTH if vertical else CANVAS_HEIGHT
            for origin in range(0, along, pitch):
                row = oriented[min(origin * len(oriented) // along, len(oriented) - 1)]
                start = 0
                while start < len(row):
                    end = start + 1
                    while end < len(row) and row[end] == row[start]:
                        end += 1
                    color = int(row[start])
                    if color != background:
                        low, high = start * across // len(row), end * across // len(row)
                        coords = (
                            (origin, low, origin, high) if vertical else (low, origin, high, origin)
                        )
                        commands.append(
                            DrawCommand.brush(color, min(size + 2, MAX_BRUSH_DIAMETER), *coords)
                        )
                    start = end
            choices.append(tuple(commands))
        shortest = min(choices, key=len)
        if len(shortest) <= budget:
            yield f'dither-{size}', shortest


def tone_plans(pixels, budget):
    # A minority-color stripe inside each cell gives intermediate tones without
    # sending a command for every dither dot. Score the actual rendered result.
    for size in TONE_CELL_SIZES:
        columns = CANVAS_WIDTH // size
        rows = CANVAS_HEIGHT // size
        plan_features, plan_costs, selections = _tone_mixtures(size)
        targets = _area_means(pixels, rows, columns)
        target_features = _features(targets.reshape(-1, 3))
        selected = []
        for start in range(0, len(target_features), MIXING_BATCH_SIZE):
            costs = -2 * target_features[start : start + MIXING_BATCH_SIZE] @ plan_features.T
            costs += plan_costs
            selected.extend(costs.argmin(axis=-1))
        choices = selections[selected].reshape(rows, columns, 3)
        background = int(bincount(choices[..., 0].ravel(), minlength=len(PALETTE)).argmax())
        commands = [] if background == 0 else [DrawCommand.fill(background)]
        for row in range(rows):
            y = (2 * row + 1) * CANVAS_HEIGHT // (2 * rows)
            start = 0
            while start < columns:
                color = int(choices[row, start, 0])
                end = start + 1
                while end < columns and choices[row, end, 0] == color:
                    end += 1
                if color != background:
                    commands.append(
                        DrawCommand.brush(
                            color,
                            min(size + 2, MAX_BRUSH_DIAMETER),
                            start * CANVAS_WIDTH // columns,
                            y,
                            end * CANVAS_WIDTH // columns,
                            y,
                        )
                    )
                start = end
        for row in range(rows):
            y = (2 * row + 1) * CANVAS_HEIGHT // (2 * rows)
            column = 0
            while column < columns:
                first, color, diameter = (int(value) for value in choices[row, column])
                end = column + 1
                while end < columns and tuple(choices[row, end]) == (first, color, diameter):
                    end += 1
                if diameter:
                    left = column * CANVAS_WIDTH // columns + diameter // 2
                    right = end * CANVAS_WIDTH // columns - diameter // 2
                    commands.append(DrawCommand.brush(color, diameter, left, y, right, y))
                column = end
        if len(commands) <= budget:
            yield f'tones-{size}', tuple(commands)
