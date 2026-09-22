"""Immutable pixel buffers with native file decoding and PNG export."""

from pathlib import Path
from asyncio import to_thread
from dataclasses import dataclass
from skribblpy.errors import ImageError
from skribblpy._image_native import (
    encode_png,
    decode_path,
    decode_bytes,
    composite_rgb,
    validate_buffer,
)


@dataclass(frozen=True, slots=True)
class RasterImage:
    """Packed row-major pixels. RGB is the default; 16-bit RGB(A) uses little endian.

    Supported modes: L, LA, RGB, RGBA, I;16, I;16L, I;16B, I;16N,
    LA;16, RGB;16, and RGBA;16. PNG export composites transparency over white.
    """

    width: int
    height: int
    pixels: bytes
    mode: str = 'RGB'

    @property
    def size(self) -> tuple[int, int]:
        return self.width, self.height

    def __post_init__(self) -> None:
        if type(self.width) is not int or type(self.height) is not int:
            raise ImageError('Image dimensions must be integers')
        if not isinstance(self.pixels, bytes):
            raise TypeError('Pixels must be immutable bytes')
        try:
            validate_buffer(self.pixels, self.width, self.height, self.mode)
        except (ValueError, OverflowError) as error:
            raise ImageError(str(error)) from error

    def tobytes(self) -> bytes:
        return self.pixels

    def getpixel(self, position: tuple[int, int]) -> tuple[int, int, int]:
        """Read an 8-bit RGB pixel from an RGB buffer."""
        if self.mode != 'RGB':
            raise ImageError('Use to_rgb() before reading RGB pixels')
        x, y = position
        if (
            type(x) is not int
            or type(y) is not int
            or not (0 <= x < self.width and 0 <= y < self.height)
        ):
            raise IndexError('Pixel position is outside the image')
        offset = (y * self.width + x) * 3
        red, green, blue = self.pixels[offset : offset + 3]
        return red, green, blue

    async def to_rgb(self) -> 'RasterImage':
        """Composite to RGB off the event loop; RGB buffers return themselves."""
        if self.mode == 'RGB':
            return self
        return await to_thread(self._to_rgb)

    async def to_png(self) -> bytes:
        """Encode RGB PNG bytes off the event loop, compositing alpha over white."""
        return await to_thread(self._to_png)

    # Keep the conventional image.save(format=...) keyword.
    # noinspection PyShadowingBuiltins
    async def save(self, path: str | Path, *, format: str = 'PNG') -> None:
        """Encode and save PNG off the event loop.

        An already running write may finish after cancellation.
        """
        await to_thread(self._save, path, format=format)

    @classmethod
    async def load(cls, path: str | Path) -> 'RasterImage':
        """Decode PNG, JPEG, or the first GIF frame off the event loop."""
        return await to_thread(cls._load, path)

    @classmethod
    async def from_encoded(cls, data: bytes) -> 'RasterImage':
        """Decode encoded image bytes off the event loop."""
        return await to_thread(cls._from_encoded, data)

    @classmethod
    async def from_pillow(cls, image) -> 'RasterImage':
        """Copy a Pillow image off the event loop without importing Pillow.

        Do not mutate the supplied image until this operation completes.
        """
        return await to_thread(cls._from_pillow, image)

    def _to_rgb(self) -> 'RasterImage':
        """Return 8-bit RGB pixels, compositing alpha over white."""
        if self.mode == 'RGB':
            return self
        pixels = composite_rgb(self.pixels, self.width, self.height, self.mode)
        return RasterImage(self.width, self.height, pixels)

    def _to_png(self) -> bytes:
        image = self._to_rgb()
        return encode_png(image.pixels, image.width, image.height)

    # Match the public save keyword.
    # noinspection PyShadowingBuiltins
    def _save(self, path: str | Path, *, format: str = 'PNG') -> None:
        """Write a PNG preview. The filename extension does not select a codec."""
        if format.upper() != 'PNG':
            raise ImageError('RasterImage export supports PNG')
        Path(path).write_bytes(self._to_png())

    @classmethod
    def _load(cls, path: str | Path) -> 'RasterImage':
        """Decode a PNG, JPEG, or the first frame of a GIF."""
        try:
            width, height, mode, pixels = decode_path(path)
        except ValueError as error:
            raise ImageError(str(error)) from error
        return cls(width, height, pixels, mode)

    @classmethod
    def _from_encoded(cls, data: bytes) -> 'RasterImage':
        try:
            width, height, mode, pixels = decode_bytes(data)
        except ValueError as error:
            raise ImageError(str(error)) from error
        return cls(width, height, pixels, mode)

    @classmethod
    def _from_pillow(cls, image) -> 'RasterImage':
        """Copy an existing Pillow image without importing or requiring Pillow."""
        if image.mode not in ('RGB', 'RGBA', 'L', 'LA', 'I;16', 'I;16L', 'I;16B', 'I;16N'):
            image = image.convert('RGBA')
        width, height = image.size
        return cls(width, height, image.tobytes(), image.mode)
