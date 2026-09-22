use crate::limits::{HEIGHT, MAX_COMMANDS, MAX_DIAMETER, MIN_DIAMETER, PALETTE_SIZE, WIDTH};

pub(crate) fn validate_commands(commands: &[Vec<i32>]) -> Result<(), String> {
    if commands.len() > MAX_COMMANDS {
        return Err("Drawing exceeds command limit".into());
    }
    for values in commands {
        if values.len() < 2 || !(0..PALETTE_SIZE as i32).contains(&values[1]) {
            return Err("Invalid command or palette index".into());
        }
        match (values[0], values.len()) {
            (1, 4) => {
                if !(0..WIDTH as i32).contains(&values[2])
                    || !(0..HEIGHT as i32).contains(&values[3])
                {
                    return Err("Fill seed is outside canvas".into());
                }
            }
            (0, 7) => {
                let diameter = values[2];
                if !(MIN_DIAMETER..=MAX_DIAMETER).contains(&diameter) {
                    return Err("Invalid brush diameter".into());
                }
                for (x, y) in [(values[3], values[4]), (values[5], values[6])] {
                    if x < -diameter
                        || x > WIDTH as i32 + diameter
                        || y < -diameter
                        || y > HEIGHT as i32 + diameter
                    {
                        return Err("Brush exceeds bounded canvas margin".into());
                    }
                }
            }
            _ => return Err("Invalid tool or command length".into()),
        }
    }
    Ok(())
}

// Shared by the planner and renderer. End offsets are exclusive.
pub(crate) fn stroke_spans(values: &[i32]) -> Vec<(usize, usize)> {
    let diameter = values[2];
    let (x, y, end_x, end_y) = (values[3], values[4], values[5], values[6]);
    let radius = diameter / 2;
    let mut spans = Vec::new();
    for delta in -radius..diameter - radius {
        if 4 * delta * delta >= diameter * diameter {
            continue;
        }
        let mut extent = 0;
        while 4 * ((extent + 1) * (extent + 1) + delta * delta) < diameter * diameter {
            extent += 1;
        }
        if y == end_y {
            let row = y + delta;
            let left = (x.min(end_x) - extent).max(0);
            let right = (x.max(end_x) + extent).min(WIDTH as i32 - 1);
            if (0..HEIGHT as i32).contains(&row) && left <= right {
                spans.push((
                    row as usize * WIDTH + left as usize,
                    row as usize * WIDTH + right as usize + 1,
                ));
            }
        } else {
            let column = x + delta;
            if !(0..WIDTH as i32).contains(&column) {
                continue;
            }
            for row in
                (y.min(end_y) - extent).max(0)..=(y.max(end_y) + extent).min(HEIGHT as i32 - 1)
            {
                let start = row as usize * WIDTH + column as usize;
                spans.push((start, start + 1));
            }
        }
    }
    spans
}

fn brush(canvas: &mut [u8], values: &[i32]) {
    let color = values[1] as u8;
    let diameter = values[2];
    let (mut x, mut y, end_x, end_y) = (values[3], values[4], values[5], values[6]);
    let radius = diameter / 2;
    if x == end_x || y == end_y {
        for (start, end) in stroke_spans(values) {
            canvas[start..end].fill(color);
        }
        return;
    }
    let dx = (end_x - x).abs();
    let dy = -(end_y - y).abs();
    let sx = if x < end_x { 1 } else { -1 };
    let sy = if y < end_y { 1 } else { -1 };
    let mut error = dx + dy;
    loop {
        for oy in -radius..diameter - radius {
            for ox in -radius..diameter - radius {
                if 4 * (ox * ox + oy * oy) < diameter * diameter
                    && (0..WIDTH as i32).contains(&(x + ox))
                    && (0..HEIGHT as i32).contains(&(y + oy))
                {
                    canvas[(y + oy) as usize * WIDTH + (x + ox) as usize] = color;
                }
            }
        }
        if x == end_x && y == end_y {
            break;
        }
        let doubled = 2 * error;
        if doubled >= dy {
            error += dy;
            x += sx;
        }
        if doubled <= dx {
            error += dx;
            y += sy;
        }
    }
}

fn flood_fill(canvas: &mut [u8], x: usize, y: usize, replacement: u8) {
    let seed = y * WIDTH + x;
    let target = canvas[seed];
    if target == replacement {
        return;
    }
    let mut pending = vec![seed];
    canvas[seed] = replacement;
    while let Some(index) = pending.pop() {
        let x = index % WIDTH;
        let y = index / WIDTH;
        for next in [
            (x > 0).then(|| index - 1),
            (x + 1 < WIDTH).then(|| index + 1),
            (y > 0).then(|| index - WIDTH),
            (y + 1 < HEIGHT).then(|| index + WIDTH),
        ]
        .into_iter()
        .flatten()
        {
            if canvas[next] == target {
                canvas[next] = replacement;
                pending.push(next);
            }
        }
    }
}

pub(crate) fn render_pixels(commands: &[Vec<i32>], palette: &[[u8; 3]]) -> Vec<u8> {
    let mut canvas = vec![0; WIDTH * HEIGHT];
    for values in commands {
        if values[0] == 1 {
            flood_fill(
                &mut canvas,
                values[2] as usize,
                values[3] as usize,
                values[1] as u8,
            );
        } else {
            brush(&mut canvas, values);
        }
    }
    let mut rgb = Vec::with_capacity(WIDTH * HEIGHT * 3);
    for index in canvas {
        rgb.extend_from_slice(&palette[index as usize]);
    }
    rgb
}
