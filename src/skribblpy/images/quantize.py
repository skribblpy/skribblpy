"""Vectorized clustered-dot and Yliluoma 1 palette selection.

The ordered matrices and mixing algorithm credit https://github.com/alekxeyuk/Skribbl.io-Bot and
https://github.com/hbldh/hitherdither under the MIT license:

Copyright (c) 2019 alekxeyuk
Copyright (c) 2020 Henrik Blidh

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

from functools import lru_cache
from numpy.typing import NDArray
from skribblpy.drawing import PALETTE
from numpy import abs, sqrt, array, empty, int64, uint8, arange, unique, float64, concatenate

CLUSTER_MATRIX = array(((12, 5, 6, 13), (4, 0, 1, 7), (11, 3, 2, 8), (15, 10, 9, 14))) / 16
BAYER_MATRIX = (
    array(
        (
            (0, 48, 12, 60, 3, 51, 15, 63),
            (32, 16, 44, 28, 35, 19, 47, 31),
            (8, 56, 4, 52, 11, 59, 7, 55),
            (40, 24, 36, 20, 43, 27, 39, 23),
            (2, 50, 14, 62, 1, 49, 13, 61),
            (34, 18, 46, 30, 33, 17, 45, 29),
            (10, 58, 6, 54, 9, 57, 5, 53),
            (42, 26, 38, 22, 41, 25, 37, 21),
        )
    )
    / 64
)
_LUMA = array((0.299, 0.587, 0.114))
_PALETTE = array(PALETTE, dtype=float64)
# Keep the palette comparison matrix near 5 MiB instead of exceeding CPU caches.
_COLOR_BATCH = 32


def _features(colors: NDArray) -> NDArray[float64]:
    normalized = colors / 255
    return concatenate((normalized * sqrt(0.75 * _LUMA), (normalized @ _LUMA)[..., None]), axis=-1)


@lru_cache(maxsize=1)
def _mixing_plans() -> tuple[NDArray[float64], NDArray[float64], NDArray[float64]]:
    colors, penalties, selections = [], [], []
    last_selection = {}
    for first in range(len(PALETTE)):
        for second in range(first, len(PALETTE)):
            ratios = arange(1 if first == second else 64, dtype=float64)
            ratios /= 64
            mixed = (
                _PALETTE[first] + ratios[:, None] * (_PALETTE[second] - _PALETTE[first])
            ).astype(uint8)
            distance = ((_features(_PALETTE[first]) - _features(_PALETTE[second])) ** 2).sum()
            for color, ratio in zip(mixed, ratios, strict=True):
                key = tuple(color)
                last_selection[key] = first, second, ratio
                colors.append(color)
                penalties.append(distance * 0.1 * (abs(ratio - 0.5) + 0.5))
                selections.append(key)
    features = _features(array(colors))
    return features, array(penalties), array([last_selection[key] for key in selections])


def cluster_dot(pixels):
    rows, columns = pixels.shape[:2]
    threshold = CLUSTER_MATRIX[arange(rows)[:, None] % 4, arange(columns)[None, :] % 4]
    adjusted = pixels.astype(float64) + threshold[..., None]
    # The pixel's squared norm is constant across palette choices.
    distances = adjusted @ _PALETTE.T
    distances *= -2
    distances += (_PALETTE**2).sum(axis=1)
    return distances.argmin(axis=-1).astype(uint8)


def yliluoma(pixels):
    colors, inverse = unique(pixels.reshape(-1, 3), axis=0, return_inverse=True)
    plans, penalties, selections = _mixing_plans()
    targets = _features(colors)
    chosen = empty(len(colors), dtype=int64)
    plan_cost = (plans**2).sum(axis=1) + penalties
    # Bound temporary matrices independently of source image size.
    for start in range(0, len(colors), _COLOR_BATCH):
        batch = targets[start : start + _COLOR_BATCH]
        # The target norm is constant within each row, so it cannot change argmin.
        errors = batch @ plans.T
        errors *= -2
        errors += plan_cost
        chosen[start : start + len(batch)] = errors.argmin(axis=1)
    selected = selections[chosen[inverse]].reshape(*pixels.shape[:2], 3)
    rows, columns = pixels.shape[:2]
    threshold = BAYER_MATRIX[arange(rows)[:, None] % 8, arange(columns)[None, :] % 8]
    return selected[..., 0].astype(uint8) * (threshold >= selected[..., 2]) + selected[
        ..., 1
    ].astype(uint8) * (threshold < selected[..., 2])
