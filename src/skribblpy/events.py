"""Ordered decorator registration, independent of the socket reader."""

from typing import TypeVar
from collections import defaultdict
from skribblpy.models import Event, EventName
from collections.abc import Callable, Awaitable
from skribblpy._callbacks import is_async_callable

type EventHandler = Callable[[Event], Awaitable[None]]
_Handler = TypeVar('_Handler', bound=EventHandler)


class EventHandlers:
    def __init__(self) -> None:
        self._handlers: dict[str, list[EventHandler]] = defaultdict(list)

    def on(self, name: str) -> Callable[[_Handler], _Handler]:
        """Register an async handler: ``@client.on(EventName.CHAT)``.

        ``EventName.ALL`` (``*``) receives every published event. Named handlers
        run first, then wildcard handlers, in registration order within each group.
        Registration returns the same callable; duplicate registrations run twice.
        Handlers must be async functions or objects with an async __call__.
        They may await client actions; CPU-heavy work should use a thread.
        """
        if not isinstance(name, str) or not name:
            raise ValueError('Event name must be a nonempty string')

        def register(handler: _Handler) -> _Handler:
            if not is_async_callable(handler):
                raise TypeError('Event handler must be an async callable; use async def')
            self._handlers[name].append(handler)
            return handler

        return register

    def off(self, name: str, handler: EventHandler) -> None:
        """Remove one registration; raise ValueError if it is not registered."""
        self._handlers[name].remove(handler)

    async def dispatch(self, event: Event) -> None:
        """Invoke handlers for an event; errors propagate to the caller."""
        named = self._handlers.get(event.name, ()) if event.name != EventName.ALL else ()
        for handler in (*named, *self._handlers.get(EventName.ALL, ())):
            await handler(event)
