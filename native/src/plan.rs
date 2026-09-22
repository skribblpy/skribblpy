use crate::limits::{HEIGHT, SAMPLE_HEIGHT, SAMPLE_WIDTH, WIDTH};
use crate::render::stroke_spans;
use std::cmp::Ordering;
use std::collections::{BinaryHeap, HashSet};

const SCALES: [usize; 6] = [38, 24, 16, 10, 6, 3];
const CHANNEL_WEIGHTS: [f64; 3] = [0.299, 0.587, 0.114];

struct Stroke {
    command: Vec<i32>,
    spans: Vec<(usize, usize)>,
}

#[derive(PartialEq)]
struct Entry {
    gain: f64,
    index: usize,
    revision: usize,
}

impl Eq for Entry {}

impl Ord for Entry {
    fn cmp(&self, other: &Self) -> Ordering {
        self.gain
            .total_cmp(&other.gain)
            .then_with(|| other.index.cmp(&self.index))
            .then_with(|| self.revision.cmp(&other.revision))
    }
}

impl PartialOrd for Entry {
    fn partial_cmp(&self, other: &Self) -> Option<Ordering> {
        Some(self.cmp(other))
    }
}

fn distance(a: &[f64; 3], b: &[u8; 3]) -> f64 {
    let mut result = 0.0;
    let mut luma = 0.0;
    for channel in 0..3 {
        let delta = a[channel] - f64::from(b[channel]);
        result += 0.75 * CHANNEL_WEIGHTS[channel] * delta * delta;
        luma += CHANNEL_WEIGHTS[channel] * delta;
    }
    result + luma * luma
}

fn candidates(pixels: &[u8], palette: &[[u8; 3]], colors: &[usize]) -> Vec<Stroke> {
    let mut strokes = Vec::new();
    let mut seen = HashSet::new();
    for size in SCALES {
        let columns = WIDTH.div_ceil(size);
        let rows = HEIGHT.div_ceil(size);
        let mut grid = vec![0; columns * rows];
        for row in 0..rows {
            for column in 0..columns {
                let mut sum = [0.0; 3];
                let mut count = 0.0;
                for y in row * size..((row + 1) * size).min(HEIGHT) {
                    for x in column * size..((column + 1) * size).min(WIDTH) {
                        let index = ((y * SAMPLE_HEIGHT as usize / HEIGHT) * SAMPLE_WIDTH as usize
                            + x * SAMPLE_WIDTH as usize / WIDTH)
                            * 3;
                        for channel in 0..3 {
                            sum[channel] += f64::from(pixels[index + channel]);
                        }
                        count += 1.0;
                    }
                }
                sum.iter_mut().for_each(|value| *value /= count);
                grid[row * columns + column] = *colors
                    .iter()
                    .min_by(|&&a, &&b| {
                        distance(&sum, &palette[a]).total_cmp(&distance(&sum, &palette[b]))
                    })
                    .unwrap();
            }
        }
        for vertical in [false, true] {
            let (lines, length) = if vertical {
                (columns, rows)
            } else {
                (rows, columns)
            };
            for line in 0..lines {
                let color_at = |offset| {
                    grid[if vertical {
                        offset * columns + line
                    } else {
                        line * columns + offset
                    }]
                };
                let mut start = 0;
                while start < length {
                    let color = color_at(start);
                    let mut end = start + 1;
                    while end < length && color_at(end) == color {
                        end += 1;
                    }
                    // Include individual cells as well as merged runs so detail can survive overlaps.
                    for (low, high) in std::iter::once((start, end))
                        .chain((start..end).map(|cell| (cell, cell + 1)))
                    {
                        let center = (line * size + size / 2) as i32;
                        let a = (low * size + size / 2) as i32;
                        let b = (high * size - size.div_ceil(2)) as i32;
                        let coordinates = if vertical {
                            [center, a, center, b]
                        } else {
                            [a, center, b, center]
                        };
                        let mut command = vec![0, color as i32, (size + 2) as i32];
                        command.extend(coordinates);
                        if seen.insert(command.clone()) {
                            let spans = stroke_spans(&command);
                            strokes.push(Stroke { command, spans });
                        }
                    }
                    start = end;
                }
            }
        }
    }
    strokes
}

pub(crate) fn plan(
    pixels: &[u8],
    palette: &[[u8; 3]],
    colors: &[usize],
    budget: usize,
) -> Vec<Vec<i32>> {
    let errors: Vec<Vec<f64>> = palette
        .iter()
        .map(|color| {
            pixels
                .as_chunks::<3>()
                .0
                .iter()
                .map(|pixel| {
                    distance(
                        &[
                            f64::from(pixel[0]),
                            f64::from(pixel[1]),
                            f64::from(pixel[2]),
                        ],
                        color,
                    )
                })
                .collect()
        })
        .collect();
    let background = *colors
        .iter()
        .min_by(|&&a, &&b| {
            errors[a]
                .iter()
                .sum::<f64>()
                .total_cmp(&errors[b].iter().sum::<f64>())
        })
        .unwrap();
    let mut commands = if background == 0 {
        Vec::new()
    } else {
        vec![vec![1, background as i32, 0, 0]]
    };
    if commands.len() == budget {
        return commands;
    }
    let samples: Vec<usize> = (0..WIDTH * HEIGHT)
        .map(|index| {
            (index / WIDTH * SAMPLE_HEIGHT as usize / HEIGHT) * SAMPLE_WIDTH as usize
                + (index % WIDTH) * SAMPLE_WIDTH as usize / WIDTH
        })
        .collect();
    let mut current: Vec<f64> = samples
        .iter()
        .map(|&index| errors[background][index])
        .collect();
    if current.iter().all(|&error| error == 0.0) {
        return commands;
    }
    let strokes = candidates(pixels, palette, colors);
    let gain = |stroke: &Stroke, current: &[f64]| -> f64 {
        let target = &errors[stroke.command[1] as usize];
        stroke
            .spans
            .iter()
            .map(|&(start, end)| {
                (start..end)
                    .map(|index| current[index] - target[samples[index]])
                    .sum::<f64>()
            })
            .sum()
    };
    let mut heap = BinaryHeap::new();
    for (index, stroke) in strokes.iter().enumerate() {
        let improvement = gain(stroke, &current);
        if improvement > 0.0 {
            heap.push(Entry {
                gain: improvement,
                index,
                revision: 0,
            });
        }
    }
    let mut revision = 0;
    while commands.len() < budget {
        let Some(entry) = heap.pop() else { break };
        let stroke = &strokes[entry.index];
        if entry.revision != revision {
            let improvement = gain(stroke, &current);
            if improvement > 0.0 {
                heap.push(Entry {
                    gain: improvement,
                    index: entry.index,
                    revision,
                });
            }
            continue;
        }
        for &(start, end) in &stroke.spans {
            for index in start..end {
                current[index] = errors[stroke.command[1] as usize][samples[index]];
            }
        }
        commands.push(stroke.command.clone());
        revision += 1;
    }
    commands
}
