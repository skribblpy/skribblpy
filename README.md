# skribblpy

An async Python framework for skribbl.io.

## What's included

- Async event handlers, plus a wildcard handler for all published events.
- Lobby management, gameplay actions, paced chat, and guess-result tracking.
- Drawing commands, canvas history, undo, and paced image playback.
- PNG, JPEG, and first-frame GIF conversion through PyO3, with optional drawing budgets.
- A word guesser that learns from revealed answers and keeps word-frequency statistics.

## Setup

You'll need **CPython 3.14 or later**. The release checks currently run on CPython 3.14 with the standard GIL build. These commands use PowerShell:

```powershell
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install skribblpy
```

Choose an extra for the parts you need:

```powershell
.venv\Scripts\python.exe -m pip install 'skribblpy[images]'
.venv\Scripts\python.exe -m pip install 'skribblpy[cli]'
```

The core depends on aiohttp. The `images` extra adds NumPy, and `cli` includes image support and argly. Pillow is only used for development fixtures and comparisons. A compatible prebuilt wheel includes the Rust extension, so installing one doesn't require a compiler.

The release build targets are:

| Platform | Architecture        | Wheel target                       |
|----------|---------------------|------------------------------------|
| Windows  | x64                 | Native Windows wheel               |
| Linux    | x64                 | manylinux with glibc 2.28 or later |
| macOS    | Intel x64           | Native Intel wheel                 |
| macOS    | Apple Silicon arm64 | Native ARM wheel                   |

Windows x64 has been validated locally. Linux and macOS builds are configured in CI and need a successful run before their artifacts are published. Other architectures, Alpine/musl, PyPy, and free-threaded Python are outside the initial test matrix.

### From the checkout

Clone [the repository](https://github.com/skribblpy/skribblpy) and run these commands in its root. Source builds need Rust and a native linker, even if you only use the core client. The checked-in toolchain file selects the Rust version:

```powershell
py -3.14 -m venv .venv
.venv\Scripts\python.exe -m pip install 'maturin>=1.15,<2'
.venv\Scripts\python.exe -m maturin develop --release --extras images,cli,dev
```

On Linux and macOS, the virtual environment's interpreter is `.venv/bin/python`. Windows source builds need the Visual Studio C++ build tools, macOS needs the Xcode command-line tools, and Linux needs a C linker. Rebuild with `maturin develop --release` after editing Rust code.

## A small client

```python
from asyncio import run
from skribblpy import Event, Client, EventName


async def main() -> None:
    client = Client(name='Python client', track_drawing=False)

    @client.on(EventName.LOBBY)
    async def joined(event: Event) -> None:
        print('Joined', event.snapshot.lobby_id)

    @client.on(EventName.CHAT)
    async def chat(event: Event) -> None:
        if message := event.chat:
            print(message)

    async with client:
        await client.connect()
        await client.wait_closed()


if __name__ == '__main__':
    run(main())
```

`connect()` uses public matchmaking. Pass a lobby code to `connect('ROOM')`, or use `connect(create_private=True)` to create a private lobby. The context manager closes the client when you leave it; entering the context doesn't connect automatically.

Register handlers before connecting. Callbacks must use `async def`; synchronous handlers and deferred text builders are rejected immediately. Constructors, decorator registration, snapshot access, and other simple data operations stay synchronous. Named handlers run first, followed by `EventName.ALL` handlers, in registration order within each group. `event.snapshot` captures state when the event arrived; `client.snapshot` holds the latest state. Use `client.off(event_name, handler)` to remove a handler.

Handlers can await client actions. Keep them short, though: later handlers wait their turn, and the event queue has a fixed capacity. Send CPU-heavy work to `asyncio.to_thread` and manage long-running tasks in your application.

## Chat and gameplay

Once connected, use `await client.send_chat('hello')` for chat or `await client.send_guess('candidate')` during an eligible guessing turn. Both share a paced queue with a minimum one-second interval.

A returned `SendReceipt` confirms a local socket write. Guess outcomes arrive separately through `event.guess_response`; an echoed incorrect guess is provisional because a close-guess notification may follow. `enqueue_chat` returns a cancellable future and also accepts an async text builder.

Gameplay methods include `choose_word`, `set_setting`, `start_game`, `vote_drawing`, and moderation actions. Use `Phase`, `SettingID`, and `GuessOutcome` for named values. Actions check the latest state and raise `ActionError` when the current role or turn doesn't allow them.

`close()` cancels pending messages. Use `close(flush=True, drain_timeout=5)` for a bounded drain. `wait_closed()` propagates transport and handler failures. Reconnection is explicit: `rejoin()` makes a fresh login to the last known lobby and keeps your handler registrations.

## Images and drawing

```python
from asyncio import run
from skribblpy.images import generate_image, render_preview


async def main() -> None:
    image = await generate_image('photo.png', preset='optimized')
    await image.save('drawing.json')
    preview = await render_preview(image)
    await preview.save('preview.png')


if __name__ == '__main__':
    run(main())
```

The available presets are:

| Preset        | What it does                                                                                          |
|---------------|-------------------------------------------------------------------------------------------------------|
| `cluster-dot` | The default conversion, with compact runs of palette colors.                                          |
| `yliluoma-1`  | Ordered dithering for intermediate tones, usually with many more commands.                            |
| `optimized`   | Compares rendered region, stripe, and dither plans to balance visual error against a supplied budget. |

**Command and duration limits default to unlimited**, within the existing 100,000-command protocol ceiling. For an explicit budget, pass `max_commands=4000`, `max_duration=15.0`, or both to `generate_image(..., preset='optimized')`. Both limits apply when supplied together. Duration counts pacing delays, not network or server latency.

`await optimize_image(...)` returns the selected image alongside candidate scores and event payload sizes. The optimizer measures drawing JSON sent to the server; PNG file size doesn't affect its choices. Preview the result to judge the tradeoff between detail and command count.

During your drawing turn, `await client.send_image(image)` clears the canvas and plays the commands. `send_drawing` plays commands without clearing. For manual drawing, use `DrawCommand.brush` and `DrawCommand.fill`; `PALETTE`, `CANVAS_WIDTH`, and `CANVAS_HEIGHT` are exported from the package. Playback uses batches of eight commands with 33 ms between batches and stops if the turn changes. Keep one playback active per client.

Image conversion, rendering, encoding, metrics, and file operations are awaitable and run off the event loop. `await render_preview(image)` returns an immutable `RasterImage`. Use `await RasterImage.load(path)`, `await image.to_rgb()`, or `await image.to_png()` for decoding and conversion. Accessing `pixels`, `size`, `tobytes()`, and `getpixel()` stays synchronous. Cancelling an image operation stops waiting; an already running worker or file write may still finish.

The same tools are available through the argly CLI:

```powershell
.venv\Scripts\python.exe -m skribblpy.cli generate --input photo.png --output drawing.json --preset optimized
.venv\Scripts\python.exe -m skribblpy.cli generate --input photo.png --output drawing.json --preset optimized --max-commands 4000
.venv\Scripts\python.exe -m skribblpy.cli preview --input drawing.json --output preview.png
```

The installed `skribbl-image` command exposes the same options. Saved JSON includes the drawing commands and image metadata. Preview rendering uses an integer approximation, so browser antialiasing can look slightly different.

## Word guesser

The [word guesser](https://github.com/skribblpy/skribblpy/tree/master/examples/word_guesser) is included as a standalone example. Run it from the checkout:

```powershell
.venv\Scripts\python.exe -m examples.word_guesser run --max-clients 2
.venv\Scripts\python.exe -m examples.word_guesser run --max-clients 1 --lobby-id ROOM --name Guesser
.venv\Scripts\python.exe -m examples.word_guesser run --help
```

It uses the bundled `words.json` and `words.stats.json` by default. Override `--database` and `--statistics` to use another dataset. Hints narrow the candidates, close guesses affect ranking, and revealed answers update the database and statistics. File writes use locks and atomic replacement so workers can share the files.

Workers leave when assigned to draw, alone in a room, or targeted by a vote-kick. Ctrl+C shuts down the pool and waits for in-progress file writes. The example lives in the checkout and isn't included in the library wheel.

## Development

```powershell
.venv\Scripts\python.exe -m pytest -q
.venv\Scripts\python.exe -m ruff check .
.venv\Scripts\python.exe -m ruff format --check .
.venv\Scripts\python.exe -m mypy
cargo fmt --manifest-path native/Cargo.toml --check
cargo clippy --manifest-path native/Cargo.toml -- -D warnings
```

The normal test suite runs offline. Core Python code lives in [`src/skribblpy`](https://github.com/skribblpy/skribblpy/tree/master/src/skribblpy), image routines in [`native/src`](https://github.com/skribblpy/skribblpy/tree/master/native/src), and comparison tools in [`validation`](https://github.com/skribblpy/skribblpy/tree/master/validation).

The **Tests** workflow runs on pull requests, pushes to `master`, manual dispatch, and calls from the release workflow. It checks Python with Ruff and mypy, checks Rust, builds each wheel from a source distribution, verifies clean core/image/CLI installations, and runs the full test suite with branch coverage against the installed wheel. Validated distributions and reports are kept as workflow artifacts for 14 days.

The **Release** workflow calls those same checks when a GitHub release is published. A stable release publishes the validated wheels and source archive to PyPI through the `pypi` environment. Prereleases and manual runs validate without publishing. Set up a [PyPI trusted publisher](https://docs.pypi.org/trusted-publishers/adding-a-publisher/) for repository `skribblpy/skribblpy`, workflow `release.yml`, and environment `pypi` before publishing your first release. No API token is needed.

To check a local release build, install the tooling extra and keep generated artifacts outside the checkout:

```powershell
.venv\Scripts\python.exe -m pip install '.[release]'
.venv\Scripts\python.exe -m maturin build --release --locked --sdist --compatibility pypi --out ..\skribblpy-release\dist
.venv\Scripts\python.exe -m validation.release check --artifacts ..\skribblpy-release\dist --work-directory ..\skribblpy-release\checks --full-suite
```

The validation command needs network access to install dependencies into temporary environments. It removes those environments when finished and keeps a `validation.json` with artifact hashes and a `coverage.xml` report when `--full-suite` is used. Version tags must match both package manifests, for example `v0.1.0`.

The framework uses the game's observed WebSocket protocol and default Socket.IO namespace. It doesn't implement polling fallback or automatic reconnection. Custom-word enforcement is still unresolved in live testing: the server accepted the settings but offered built-in words.

## License

[MIT](https://github.com/skribblpy/skribblpy/blob/master/LICENSE). Existing dither-algorithm attributions are retained in [`quantize.py`](https://github.com/skribblpy/skribblpy/blob/master/src/skribblpy/images/quantize.py), and bundled Rust dependency notices are included in [`_native_licenses.json`](https://github.com/skribblpy/skribblpy/blob/master/src/skribblpy/_native_licenses.json).
