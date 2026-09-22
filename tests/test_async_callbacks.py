from functools import partial
from pytest import mark, raises
from tests.test_client import LocalServer
from asyncio import sleep, create_task, CancelledError
from skribblpy import Event, Client, Snapshot, EventName


@mark.parametrize('handler', [lambda event: None, [].append, object(), None])
def test_sync_handlers_rejected_before_registration(handler):
    client = Client()
    with raises(TypeError, match='async callable'):
        # Exercise runtime rejection of invalid handlers.
        # noinspection PyTypeChecker
        client.on(EventName.CHAT)(handler)
    assert not client._handlers


async def test_async_functions_methods_partials_and_callable_instances_keep_order():
    client = Client()
    seen = []

    class Handler:
        async def __call__(self, event):
            seen.append(('callable', event.sequence))

        async def method(self, event):
            seen.append(('method', event.sequence))

    async def record(label, event):
        await sleep(0)
        seen.append((label, event.sequence))

    handler = Handler()
    client.on(EventName.ALL)(partial(record, 'wildcard'))
    # Exercise runtime rejection of invalid handlers.
    # noinspection PyTypeChecker
    client.on(EventName.CHAT)(handler)
    client.on(EventName.CHAT)(handler.method)
    client.on(EventName.CHAT)(partial(record, 'partial'))
    await client.dispatch(Event(EventName.CHAT, Snapshot(), 1, 0))
    assert seen == [('callable', 1), ('method', 1), ('partial', 1), ('wildcard', 1)]
    with raises(TypeError, match='async callable'):
        # A callable class must be rejected instead of registered.
        # noinspection PyTypeChecker
        client.on(EventName.CHAT)(Handler)


async def test_async_generators_and_sync_coroutine_factories_are_rejected():
    client = Client()
    called = False

    async def coroutine(_event):
        pass

    async def generator(event):
        yield event

    def factory(event):
        nonlocal called
        called = True
        return coroutine(event)

    for handler in (generator, factory):
        with raises(TypeError, match='async callable'):
            # Exercise runtime rejection of invalid handlers.
            # noinspection PyTypeChecker
            client.on(EventName.CHAT)(handler)
    assert not called


async def test_builders_require_async_callables_and_validate_result(monkeypatch, lobby):
    async with LocalServer(monkeypatch, lobby) as server:
        async with Client(base_url=server.base_url) as client:
            await client.connect()
            called = False

            def sync_builder():
                nonlocal called
                called = True
                return 'not sent'

            with raises(TypeError, match='async callable'):
                # Exercise runtime rejection of a synchronous builder.
                # noinspection PyTypeChecker
                rejected = client.enqueue_chat(sync_builder)
                rejected.cancel()
            with raises(TypeError, match='async callable'):
                # Exercise runtime rejection of a synchronous builder.
                # noinspection PyTypeChecker
                await client.send_guess(sync_builder)
            sender = client._chat
            assert sender is not None
            assert not called and sender.queue.empty()

            async def invalid_builder():
                return 123

            with raises(ValueError, match='Chat must contain'):
                # Exercise validation of the awaited result.
                # noinspection PyTypeChecker
                await client.send_chat(invalid_builder)

            class Builder:
                async def __call__(self):
                    return 'async builder'

            receipt = await client.send_chat(Builder())
            assert receipt.text == 'async builder'


async def test_cancellation_reaches_an_awaited_handler():
    client = Client()
    entered, cancelled = [], []

    @client.on(EventName.CHAT)
    async def handler(event):
        entered.append(event.sequence)
        try:
            await sleep(60)
        finally:
            cancelled.append(event.sequence)

    task = create_task(client.dispatch(Event(EventName.CHAT, Snapshot(), 1, 0)))
    await sleep(0)
    task.cancel()
    with raises(CancelledError):
        await task
    assert entered == cancelled == [1]
