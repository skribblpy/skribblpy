"""Validation shared by event handlers and deferred text builders."""

from inspect import iscoroutinefunction


def is_async_callable(callback: object) -> bool:
    return (
        callable(callback)
        and not isinstance(callback, type)
        and (iscoroutinefunction(callback) or iscoroutinefunction(type(callback).__call__))
    )
