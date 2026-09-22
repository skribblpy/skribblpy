pub(crate) const WIDTH: usize = 800;
pub(crate) const HEIGHT: usize = 600;
pub(crate) const SAMPLE_WIDTH: u32 = 200;
pub(crate) const SAMPLE_HEIGHT: u32 = 150;
pub(crate) const MAX_PIXELS: u64 = 100_000_000;
pub(crate) const MAX_ENCODED_BYTES: usize = 64 << 20;
pub(crate) const MAX_COMMANDS: usize = 100_000;
pub(crate) const MIN_DIAMETER: i32 = 4;
pub(crate) const MAX_DIAMETER: i32 = 40;
pub(crate) const PALETTE_SIZE: usize = 26;

pub(crate) fn dimensions(width: u32, height: u32) -> Result<usize, String> {
    let count = u64::from(width) * u64::from(height);
    if count == 0 || count > MAX_PIXELS {
        return Err("Source dimensions exceed image limits".into());
    }
    Ok(count as usize)
}
