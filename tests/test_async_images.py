from pytest import mark, raises
from threading import get_ident
from importlib import import_module
from threading import Event as ThreadEvent
from skribblpy.images import ImageData, RasterImage, generate_image
from asyncio import (
    Event,
    sleep,
    gather,
    timeout,
    to_thread,
    create_task,
    CancelledError,
    get_running_loop,
)


@mark.parametrize(
    'operation',
    [
        'generate',
        'optimize',
        'render',
        'payload',
        'quality',
        'drawing-save',
        'drawing-load',
        'raster-save',
        'raster-load',
        'rgb',
        'png',
        'decode',
        'pillow',
    ],
)
async def test_blocking_image_work_runs_outside_the_event_loop(monkeypatch, operation, tmp_path):
    entered, release = Event(), ThreadEvent()
    loop = get_running_loop()
    thread_ids = []
    result = object()

    def worker(_owner=None, *_args, **_kwargs):
        thread_ids.append(get_ident())
        loop.call_soon_threadsafe(entered.set)
        assert release.wait(3), 'Event loop did not release the worker'
        return result

    image = RasterImage(1, 1, b'\0\0\0\0', 'RGBA')
    data = ImageData((), 1, 1, 1, 1)
    functions = {
        'generate': ('generate', 'generate_image', (image,)),
        'optimize': ('optimize', 'optimize_image', (image,)),
        'render': ('render', 'render_preview', (data,)),
        'payload': ('metrics', 'drawing_payload_bytes', (data,)),
        'quality': ('metrics', 'visual_error', (image, image)),
    }
    methods = {
        'drawing-save': (data, 'save', (tmp_path / 'drawing.json',)),
        'drawing-load': (ImageData, 'load', (tmp_path / 'drawing.json',)),
        'raster-save': (image, 'save', (tmp_path / 'image.png',)),
        'raster-load': (RasterImage, 'load', (tmp_path / 'image.png',)),
        'rgb': (image, 'to_rgb', ()),
        'png': (image, 'to_png', ()),
        'decode': (RasterImage, 'from_encoded', (b'encoded',)),
        'pillow': (RasterImage, 'from_pillow', (object(),)),
    }
    if operation in functions:
        module, name, arguments = functions[operation]
        owner = import_module(f'skribblpy.images.{module}')
        monkeypatch.setattr(owner, '_' + name, worker)
    else:
        owner, name, arguments = methods[operation]
        cls = owner if isinstance(owner, type) else type(owner)
        replacement = classmethod(worker) if isinstance(owner, type) else worker
        monkeypatch.setattr(cls, '_' + name, replacement)

    task = create_task(getattr(owner, name)(*arguments))
    try:
        async with timeout(2):
            await entered.wait()
            assert len(thread_ids) == 1 and thread_ids[0] != get_ident()
            # The worker is still blocked while this task gets more loop turns.
            for _ in range(3):
                await sleep(0)
                assert not task.done()
            release.set()
            actual = await task
            assert actual is (None if name == 'save' else result)
    finally:
        release.set()
        await gather(task, return_exceptions=True)


async def test_cancelling_conversion_does_not_block_or_cancel_other_tasks(monkeypatch):
    entered, release, finished = Event(), ThreadEvent(), ThreadEvent()
    loop = get_running_loop()

    def worker(*_args, **_kwargs):
        loop.call_soon_threadsafe(entered.set)
        try:
            assert release.wait(3)
            return ImageData((), 1, 1, 1, 1)
        finally:
            finished.set()

    monkeypatch.setattr('skribblpy.images.generate._generate_image', worker)
    task = create_task(generate_image(RasterImage(1, 1, b'\0\0\0')))
    try:
        async with timeout(2):
            await entered.wait()
            task.cancel()
            with raises(CancelledError):
                await task
            assert not finished.is_set()
            await sleep(0)
    finally:
        release.set()
        assert await to_thread(finished.wait, 3)
        await gather(task, return_exceptions=True)
