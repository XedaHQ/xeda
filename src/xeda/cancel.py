"""Cancel a launch from another thread, or after a time.

A `CancelToken` is the request to stop. A launch observes it while it waits for a lock, generates
sources, runs a tool and, with a remote host, follows the launch there. `launch_canceled` is how
the code of one launch asks, without a token in its hands.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
from time import monotonic

__all__ = ["CancelToken", "launch_canceled"]


class CancelToken:
    """A request to stop one or more launches.

    `cancel()` is safe from any thread, and calling it again does nothing. `canceled` is true
    after `cancel()`, and once `timeout` seconds have passed since the construction of the token.
    The timeout counts from the construction, not from the start of a launch: a token that several
    launches share cancels all of them when it expires. Make one token for each launch to give
    each its own time."""

    def __init__(self, *, timeout: float | None = None) -> None:
        # `not timeout > 0` also refuses NaN.
        if timeout is not None and not timeout > 0:
            raise ValueError(f"`timeout` is a positive number of seconds, not {timeout!r}")
        self._event = threading.Event()
        self._expires = None if timeout is None else monotonic() + timeout

    def cancel(self) -> None:
        """Ask the launches that observe this token to stop."""
        self._event.set()

    @property
    def canceled(self) -> bool:
        """Whether `cancel()` was called or the timeout has passed."""
        return self._event.is_set() or (self._expires is not None and monotonic() >= self._expires)

    def __repr__(self) -> str:
        return f"CancelToken(canceled={self.canceled})"


#: What a launch that runs in this context asks to know if it must stop. `None` outside a launch.
_PREDICATE: ContextVar[Callable[[], bool] | None] = ContextVar("xeda_launch_canceled", default=None)


def launch_canceled() -> bool:
    """Return whether the launch that runs in this context must stop. Outside a launch: no."""
    predicate = _PREDICATE.get()
    return predicate is not None and predicate()


@contextmanager
def observing(predicate: Callable[[], bool]) -> Iterator[None]:
    """Make `launch_canceled` answer with `predicate` in this context, for the block."""
    token = _PREDICATE.set(predicate)
    try:
        yield
    finally:
        _PREDICATE.reset(token)
