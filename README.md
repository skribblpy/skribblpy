# skribblpy

A modern Python framework for skribbl.io, built around asyncio and decorator-based event handlers.

- Manage lobbies, react to game events, and send paced chat and guesses.
- Turn images into drawing commands with a Rust backend, then preview and play them back.
- Try the included word guesser, which learns from revealed answers and tracks word frequencies.

Requires **CPython 3.14 or later**. See [Getting started](https://github.com/skribblpy/skribblpy/wiki/Getting-started) for installation, optional image/CLI extras, and source builds.

## A small client

```python
from asyncio import run
from skribblpy import Event, Client, EventName


async def main() -> None:
    client = Client(name='Python client', track_drawing=False)

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

This joins through public matchmaking. Use `connect('ROOM')` for a specific lobby or `connect(create_private=True)` to create one. Handlers use `async def`; the context manager handles cleanup.

## Guides

The [wiki](https://github.com/skribblpy/skribblpy/wiki) has the details:

- [Events and lifecycle](https://github.com/skribblpy/skribblpy/wiki/Events-and-lifecycle)
- [Chat and gameplay](https://github.com/skribblpy/skribblpy/wiki/Chat-and-gameplay)
- [Images, drawing budgets, and the CLI](https://github.com/skribblpy/skribblpy/wiki/Images-and-drawing)
- [Word guesser](https://github.com/skribblpy/skribblpy/wiki/Word-guesser)
- [Troubleshooting and current limits](https://github.com/skribblpy/skribblpy/wiki/Troubleshooting)
- [Development and releases](https://github.com/skribblpy/skribblpy/wiki/Development-and-releases)

Found a bug? [Open an issue](https://github.com/skribblpy/skribblpy/issues).

## License

[MIT](https://github.com/skribblpy/skribblpy/blob/master/LICENSE). Third-party notices are included with the source and packaged distribution.
