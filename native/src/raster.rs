use crate::limits::{SAMPLE_HEIGHT, SAMPLE_WIDTH, dimensions};

#[derive(Clone, Copy)]
pub(crate) struct Layout {
    pub(crate) channels: usize,
    pub(crate) depth: usize,
    pub(crate) big_endian: bool,
}

impl Layout {
    pub(crate) fn parse(mode: &str) -> Result<Self, String> {
        let (channels, depth, big_endian) = match mode {
            "L" => (1, 1, false),
            "LA" => (2, 1, false),
            "RGB" => (3, 1, false),
            "RGBA" => (4, 1, false),
            "I;16" | "I;16L" => (1, 2, false),
            "I;16B" => (1, 2, true),
            "I;16N" => (1, 2, cfg!(target_endian = "big")),
            "LA;16" => (2, 2, false),
            "RGB;16" => (3, 2, false),
            "RGBA;16" => (4, 2, false),
            _ => return Err(format!("Unsupported pixel mode: {mode}")),
        };
        Ok(Self {
            channels,
            depth,
            big_endian,
        })
    }

    pub(crate) fn validate(self, pixels: &[u8], width: u32, height: u32) -> Result<(), String> {
        let count = dimensions(width, height)?;
        if count * self.channels * self.depth != pixels.len() {
            return Err("Pixel buffer length does not match dimensions and mode".into());
        }
        Ok(())
    }

    pub(crate) fn rgba(self, pixels: &[u8], index: usize) -> [u16; 4] {
        let start = index * self.channels * self.depth;
        let channel = |number: usize| {
            let offset = start + number * self.depth;
            if self.depth == 1 {
                u16::from(pixels[offset]) * 257
            } else {
                let bytes = [pixels[offset], pixels[offset + 1]];
                if self.big_endian {
                    u16::from_be_bytes(bytes)
                } else {
                    u16::from_le_bytes(bytes)
                }
            }
        };
        match self.channels {
            1 => [channel(0), channel(0), channel(0), u16::MAX],
            2 => [channel(0), channel(0), channel(0), channel(1)],
            3 => [channel(0), channel(1), channel(2), u16::MAX],
            _ => [channel(0), channel(1), channel(2), channel(3)],
        }
    }
}

fn composite(rgba: [u16; 4]) -> [u8; 3] {
    let alpha = u64::from(rgba[3]);
    [rgba[0], rgba[1], rgba[2]].map(|channel| {
        let premultiplied = u64::from(channel) * alpha / 65535;
        ((premultiplied + 65535 - alpha + 128) / 257) as u8
    })
}

pub(crate) fn sample(pixels: &[u8], width: u32, height: u32, layout: Layout) -> Vec<u8> {
    let mut output = Vec::with_capacity((SAMPLE_WIDTH * SAMPLE_HEIGHT * 3) as usize);
    for row in 0..SAMPLE_HEIGHT {
        let y = ((2 * u64::from(row) + 1) * u64::from(height) / (2 * u64::from(SAMPLE_HEIGHT)))
            as usize;
        for column in 0..SAMPLE_WIDTH {
            let x = ((2 * u64::from(column) + 1) * u64::from(width) / (2 * u64::from(SAMPLE_WIDTH)))
                as usize;
            output.extend_from_slice(&composite(layout.rgba(pixels, y * width as usize + x)));
        }
    }
    output
}

pub(crate) fn rgb(pixels: &[u8], width: u32, height: u32, layout: Layout) -> Vec<u8> {
    let count = width as usize * height as usize;
    let mut output = Vec::with_capacity(count * 3);
    for index in 0..count {
        output.extend_from_slice(&composite(layout.rgba(pixels, index)));
    }
    output
}
