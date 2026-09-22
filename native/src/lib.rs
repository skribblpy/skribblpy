mod codec;
mod limits;
mod plan;
mod raster;
mod render;

use pyo3::exceptions::PyValueError;
use pyo3::prelude::*;
use pyo3::types::PyBytes;
use std::path::PathBuf;

type ImageResult<'py> = (u32, u32, &'static str, Bound<'py, PyBytes>);

fn checked_layout(pixels: &[u8], width: u32, height: u32, mode: &str) -> PyResult<raster::Layout> {
    let layout = raster::Layout::parse(mode).map_err(PyValueError::new_err)?;
    layout
        .validate(pixels, width, height)
        .map_err(PyValueError::new_err)?;
    Ok(layout)
}

#[pyfunction]
fn validate_buffer(pixels: &[u8], width: u32, height: u32, mode: &str) -> PyResult<()> {
    checked_layout(pixels, width, height, mode)?;
    Ok(())
}

#[pyfunction]
fn decode_path(py: Python<'_>, path: PathBuf) -> PyResult<ImageResult<'_>> {
    let decoded = py.detach(|| codec::load(&path))?;
    Ok((
        decoded.width,
        decoded.height,
        decoded.mode,
        PyBytes::new(py, &decoded.pixels),
    ))
}

#[pyfunction]
fn decode_bytes<'py>(py: Python<'py>, data: &[u8]) -> PyResult<ImageResult<'py>> {
    let decoded = py.detach(|| codec::from_bytes(data))?;
    Ok((
        decoded.width,
        decoded.height,
        decoded.mode,
        PyBytes::new(py, &decoded.pixels),
    ))
}

#[pyfunction]
fn sample_path(py: Python<'_>, path: PathBuf) -> PyResult<(u32, u32, Bound<'_, PyBytes>)> {
    let (width, height, pixels) = py.detach(|| {
        let decoded = codec::load(&path)?;
        let layout = checked_layout(&decoded.pixels, decoded.width, decoded.height, decoded.mode)?;
        let pixels = raster::sample(&decoded.pixels, decoded.width, decoded.height, layout);
        Ok::<_, PyErr>((decoded.width, decoded.height, pixels))
    })?;
    Ok((width, height, PyBytes::new(py, &pixels)))
}

#[pyfunction]
fn sample_buffer<'py>(
    py: Python<'py>,
    pixels: &[u8],
    width: u32,
    height: u32,
    mode: &str,
) -> PyResult<Bound<'py, PyBytes>> {
    let layout = checked_layout(pixels, width, height, mode)?;
    let output = py.detach(|| raster::sample(pixels, width, height, layout));
    Ok(PyBytes::new(py, &output))
}

#[pyfunction]
fn composite_rgb<'py>(
    py: Python<'py>,
    pixels: &[u8],
    width: u32,
    height: u32,
    mode: &str,
) -> PyResult<Bound<'py, PyBytes>> {
    let layout = checked_layout(pixels, width, height, mode)?;
    let output = py.detach(|| raster::rgb(pixels, width, height, layout));
    Ok(PyBytes::new(py, &output))
}

#[pyfunction(name = "render")]
fn render_commands(
    py: Python<'_>,
    commands: Vec<Vec<i32>>,
    palette: Vec<[u8; 3]>,
) -> PyResult<Bound<'_, PyBytes>> {
    if palette.len() != limits::PALETTE_SIZE {
        return Err(PyValueError::new_err("Expected the protocol palette"));
    }
    render::validate_commands(&commands).map_err(PyValueError::new_err)?;
    let output = py.detach(|| render::render_pixels(&commands, &palette));
    Ok(PyBytes::new(py, &output))
}

#[pyfunction]
fn encode_png<'py>(
    py: Python<'py>,
    pixels: &[u8],
    width: u32,
    height: u32,
) -> PyResult<Bound<'py, PyBytes>> {
    checked_layout(pixels, width, height, "RGB")?;
    let output = py.detach(|| codec::png(pixels, width, height))?;
    Ok(PyBytes::new(py, &output))
}

#[pyfunction]
fn plan_strokes(
    py: Python<'_>,
    pixels: &[u8],
    palette: Vec<[u8; 3]>,
    colors: Vec<usize>,
    budget: usize,
) -> PyResult<Vec<Vec<i32>>> {
    if pixels.len() != (limits::SAMPLE_WIDTH * limits::SAMPLE_HEIGHT * 3) as usize
        || palette.len() != limits::PALETTE_SIZE
        || colors.is_empty()
        || colors.len() > limits::PALETTE_SIZE
        || colors.iter().any(|&index| index >= limits::PALETTE_SIZE)
        || budget == 0
        || budget > limits::MAX_COMMANDS
    {
        return Err(PyValueError::new_err(
            "Invalid planner pixels, palette, or budget",
        ));
    }
    Ok(py.detach(|| plan::plan(pixels, &palette, &colors, budget)))
}

#[pymodule]
fn _image_native(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add_function(wrap_pyfunction!(plan_strokes, module)?)?;
    module.add_function(wrap_pyfunction!(validate_buffer, module)?)?;
    module.add_function(wrap_pyfunction!(decode_path, module)?)?;
    module.add_function(wrap_pyfunction!(decode_bytes, module)?)?;
    module.add_function(wrap_pyfunction!(sample_path, module)?)?;
    module.add_function(wrap_pyfunction!(sample_buffer, module)?)?;
    module.add_function(wrap_pyfunction!(composite_rgb, module)?)?;
    module.add_function(wrap_pyfunction!(render_commands, module)?)?;
    module.add_function(wrap_pyfunction!(encode_png, module)?)?;
    module.add("SAMPLE_WIDTH", limits::SAMPLE_WIDTH)?;
    module.add("SAMPLE_HEIGHT", limits::SAMPLE_HEIGHT)?;
    module.add("MAX_SOURCE_PIXELS", limits::MAX_PIXELS)?;
    module.add("MAX_ENCODED_BYTES", limits::MAX_ENCODED_BYTES)?;
    Ok(())
}
