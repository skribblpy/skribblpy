use crate::limits::{MAX_ENCODED_BYTES, MAX_PIXELS, dimensions};
use image::codecs::png::{CompressionType, FilterType, PngEncoder};
use image::{DynamicImage, ImageDecoder, ImageEncoder, ImageFormat, ImageReader, Limits};
use pyo3::exceptions::PyValueError;
use pyo3::{PyErr, PyResult};
use std::fs::File;
use std::io::{BufRead, BufReader, Cursor, Seek};
use std::path::Path;

pub(crate) struct Decoded {
    pub(crate) width: u32,
    pub(crate) height: u32,
    pub(crate) mode: &'static str,
    pub(crate) pixels: Vec<u8>,
}

fn image_error(error: image::ImageError) -> PyErr {
    match error {
        image::ImageError::IoError(error)
            if matches!(
                error.kind(),
                std::io::ErrorKind::UnexpectedEof | std::io::ErrorKind::InvalidData
            ) =>
        {
            PyValueError::new_err(error.to_string())
        }
        image::ImageError::IoError(error) => error.into(),
        other => PyValueError::new_err(other.to_string()),
    }
}

fn decode<R: BufRead + Seek>(mut reader: ImageReader<R>) -> PyResult<Decoded> {
    if !matches!(
        reader.format(),
        Some(ImageFormat::Png | ImageFormat::Jpeg | ImageFormat::Gif)
    ) {
        return Err(PyValueError::new_err("Expected PNG, JPEG, or GIF input"));
    }
    let mut limits = Limits::default();
    limits.max_alloc = Some(MAX_PIXELS * 8);
    reader.limits(limits);
    let decoder = reader.into_decoder().map_err(image_error)?;
    let (width, height) = decoder.dimensions();
    dimensions(width, height).map_err(PyValueError::new_err)?;
    let decoded = DynamicImage::from_decoder(decoder).map_err(image_error)?;
    let (mode, pixels) = match decoded {
        DynamicImage::ImageLuma8(value) => ("L", value.into_raw()),
        DynamicImage::ImageLumaA8(value) => ("LA", value.into_raw()),
        DynamicImage::ImageRgb8(value) => ("RGB", value.into_raw()),
        DynamicImage::ImageRgba8(value) => ("RGBA", value.into_raw()),
        DynamicImage::ImageLuma16(value) => ("I;16", little_endian(value.into_raw())),
        DynamicImage::ImageLumaA16(value) => ("LA;16", little_endian(value.into_raw())),
        DynamicImage::ImageRgb16(value) => ("RGB;16", little_endian(value.into_raw())),
        DynamicImage::ImageRgba16(value) => ("RGBA;16", little_endian(value.into_raw())),
        _ => return Err(PyValueError::new_err("Unsupported decoded pixel format")),
    };
    Ok(Decoded {
        width,
        height,
        mode,
        pixels,
    })
}

fn little_endian(values: Vec<u16>) -> Vec<u8> {
    values.into_iter().flat_map(u16::to_le_bytes).collect()
}

pub(crate) fn load(path: &Path) -> PyResult<Decoded> {
    let file = File::open(path)?;
    if file.metadata()?.len() > MAX_ENCODED_BYTES as u64 {
        return Err(PyValueError::new_err("Encoded image exceeds size limit"));
    }
    decode(ImageReader::new(BufReader::new(file)).with_guessed_format()?)
}

pub(crate) fn from_bytes(data: &[u8]) -> PyResult<Decoded> {
    if data.len() > MAX_ENCODED_BYTES {
        return Err(PyValueError::new_err("Encoded image exceeds size limit"));
    }
    decode(ImageReader::new(Cursor::new(data)).with_guessed_format()?)
}

pub(crate) fn png(pixels: &[u8], width: u32, height: u32) -> PyResult<Vec<u8>> {
    let mut output = Vec::new();
    PngEncoder::new_with_quality(&mut output, CompressionType::Fast, FilterType::Adaptive)
        .write_image(pixels, width, height, image::ExtendedColorType::Rgb8)
        .map_err(image_error)?;
    Ok(output)
}
