"""What a block of code writes, seen by the audit hook: the files, links and directories it
creates, changes, renames or deletes in this process (not what a child process does).

The events and how they name a path are `tests/test_isolation.py`'s (`_written`); this is the
hook for a test that wants the list of writes of one block, rather than the isolation sweep's
verdict on a launch.
"""

import sys
from contextlib import contextmanager
from typing import Iterator

from .test_isolation import _written

_SINKS: list[list[str]] = []


def _audit(event: str, args: tuple) -> None:
    if _SINKS:
        for path in _written(event, args):
            for sink in _SINKS:
                sink.append(f"{event} {path}")


sys.addaudithook(_audit)


@contextmanager
def writes_seen(into: list[str]) -> Iterator[list[str]]:
    """While the block runs, append `event path` to `into` for each write this process makes."""
    _SINKS.append(into)
    try:
        yield into
    finally:
        _SINKS.remove(into)
