"""Tell the log records of a launch from the records of any other code.

A launch keeps its id and the name of the node it runs in two context variables. A context
variable belongs to the thread (or task) that sets it, so two launches that run at once in two
threads of one process never see each other's values.

`LaunchLogFilter` copies both values onto a log record as the attributes `xeda_launch_id` (a
string, or `None` outside a launch) and `xeda_node` (a string, or `None` when no node runs). The
package installs it on the one handler it adds to the `xeda` logger (`TaggingNullHandler`, in
`xeda/__init__.py`). A logger calls the handlers of its parents in order, from the logger up to the
root, and the `xeda` logger comes before the root. So the record has both attributes when the
handlers of the application see it, and Xeda sets no record factory, adds no handler that writes,
and changes no level. A handler that an application puts on a logger below `xeda` runs before the
tag exists. Such a handler adds `LaunchLogFilter()` itself.
"""

from __future__ import annotations

import functools
import logging
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any, TypeVar

__all__ = [
    "LAUNCH_ID",
    "NODE",
    "LaunchLogFilter",
    "TaggingNullHandler",
    "current_launch_id",
    "launch_context",
    "launch_id_of",
    "running_node",
    "tag_launch_id",
]

#: The id of the launch that runs in this context (a `uuid4` as 32 hexadecimal digits), or `None`.
LAUNCH_ID: ContextVar[str | None] = ContextVar("xeda_launch_id", default=None)
#: The label of the node that runs in this context, or `None`.
NODE: ContextVar[str | None] = ContextVar("xeda_node", default=None)


def current_launch_id() -> str | None:
    """Return the id of the launch that runs in this context, or `None` outside a launch."""
    return LAUNCH_ID.get()


@contextmanager
def launch_context() -> Iterator[str]:
    """Run a launch under its id and return that id.

    A launch that starts inside another one in the same context (a launch that a call of
    `run_flow` makes for each producer) keeps the id of the outer one."""
    existing = LAUNCH_ID.get()
    if existing is not None:
        yield existing
        return
    launch_id = uuid.uuid4().hex
    token = LAUNCH_ID.set(launch_id)
    try:
        yield launch_id
    finally:
        LAUNCH_ID.reset(token)


def tag_launch_id(error: BaseException, launch_id: str) -> None:
    """Note on `error`, which leaves a launch, the id of that launch (the first launch to tag it)."""
    if not hasattr(error, "xeda_launch_id"):
        try:
            error.xeda_launch_id = launch_id  # type: ignore[attr-defined]
        except AttributeError:  # an exception that takes no attribute
            pass


def launch_id_of(error: BaseException) -> str | None:
    """Return the id of the launch that `error` left (`tag_launch_id`), or None."""
    found = getattr(error, "xeda_launch_id", None)
    return found if isinstance(found, str) else None


@contextmanager
def running_node(label: str) -> Iterator[None]:
    """Run a node under its label."""
    token = NODE.set(label)
    try:
        yield
    finally:
        NODE.reset(token)


class LaunchLogFilter(logging.Filter):
    """Put the launch id and the node label of the context on each record. It never drops one."""

    def filter(self, record: logging.LogRecord) -> bool:
        record.xeda_launch_id = LAUNCH_ID.get()
        record.xeda_node = NODE.get()
        return True


class TaggingNullHandler(logging.NullHandler):
    """The `NullHandler` of the `xeda` logger. It writes nothing, and tags each record that
    reaches it with `LaunchLogFilter`."""

    def __init__(self) -> None:
        super().__init__()
        self.addFilter(LaunchLogFilter())

    def handle(self, record: logging.LogRecord) -> bool:
        self.filter(record)
        return False


_Method = TypeVar("_Method", bound=Callable[..., Any])


def with_launch_context(method: _Method) -> _Method:
    """Run a method of a launcher under a launch id (`launch_context`)."""

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        with launch_context():
            return method(*args, **kwargs)

    return wrapper  # type: ignore[return-value]
