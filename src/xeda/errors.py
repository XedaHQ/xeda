"""The errors that Xeda raises to a caller.

Every error has a stable `code`: a lower snake case text that a script can compare. The codes
never follow class names. Their set is open: a release may add a code, and a reader keeps an
unknown code as text.

`SpecError` and its subclasses mean that the request is wrong: nothing ran. The other errors
mean that a launch, a record, a lease or a remote host failed.
"""

from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path
from typing import Any

from .utils import XedaException

__all__ = [
    "XedaError",
    "SpecError",
    "SettingsError",
    "DesignError",
    "ProjectError",
    "FlowNotFoundError",
    "ChainError",
    "RunRootError",
    "StalePlanError",
    "RecordError",
    "RunReplacedError",
    "RemoteError",
    "RunFailedError",
    "LeaseHeldError",
]


def _rebuild(cls: type[XedaError], message: str, state: dict[str, Any]) -> XedaError:
    """Rebuild an error that was pickled: its fields are its `__dict__`."""
    error = Exception.__new__(cls)
    Exception.__init__(error, message)
    error.__dict__.update(state)
    return error


class XedaError(XedaException):
    """The base class of every error that Xeda raises to a caller.

    `code` is the stable code of the failure, and `message` is the text for a person."""

    def __init__(self, message: str, *, code: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message

    def __reduce__(self) -> tuple[Any, ...]:  # an exception crosses a process boundary by pickling
        return (_rebuild, (type(self), self.message, dict(self.__dict__)))


class SpecError(XedaError):
    """The request is wrong. Nothing ran.

    `points` holds the indexes of the sweep points that the error is about, and is empty for
    any other request."""

    def __init__(self, message: str, *, code: str, points: Iterable[int] = ()) -> None:
        super().__init__(message, code=code)
        self.points = tuple(points)


class SettingsError(SpecError):
    """A setting is wrong.

    `node` is the node that the setting addresses, `None` when the key addresses none. `setting`
    and `suggestions` are addresses below `node`, dotted for the leaf of a group. When `node` is
    `None`, they are full addresses. `errors` holds every finding of one validation, in address
    order. This exception is the first of them, so a script that reads only the exception sees
    the first mistake. `origin` names the layer that gave the setting."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        node: str | None = None,
        setting: str | None = None,
        problem: str | None = None,
        suggestions: Iterable[str] = (),
        origin: str | None = None,
        errors: Iterable[SettingsError] = (),
        points: Iterable[int] = (),
    ) -> None:
        super().__init__(message, code=code, points=points)
        self.node = node
        self.setting = setting
        self.problem = message if problem is None else problem
        self.suggestions = tuple(suggestions)
        self.origin = origin
        self.errors: tuple[SettingsError, ...] = tuple(errors) or (self,)


class DesignError(SpecError):
    """The design is wrong, or the file of the design is missing or unreadable."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        field: str | None = None,
        problem: str | None = None,
        path: Path | None = None,
        points: Iterable[int] = (),
    ) -> None:
        super().__init__(message, code=code, points=points)
        self.field = field
        self.problem = message if problem is None else problem
        self.path = path


class ProjectError(SpecError):
    """The project file is missing, unreadable or wrong, or it does not hold the design."""

    def __init__(
        self, message: str, *, code: str, path: Path | None = None, points: Iterable[int] = ()
    ) -> None:
        super().__init__(message, code=code, points=points)
        self.path = path


class FlowNotFoundError(SpecError):
    """The name of a flow is no flow. `suggestions` holds the close names."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        name: str,
        suggestions: Iterable[str] = (),
        points: Iterable[int] = (),
    ) -> None:
        super().__init__(message, code=code, points=points)
        self.name = name
        self.suggestions = tuple(suggestions)


class ChainError(SpecError):
    """A chain of flows is wrong. `chain` is the text of the chain."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        chain: str,
        problem: str | None = None,
        points: Iterable[int] = (),
    ) -> None:
        super().__init__(message, code=code, points=points)
        self.chain = chain
        self.problem = message if problem is None else problem


class RunRootError(SpecError):
    """The directory named as the run root is not one that Xeda can take for its own."""

    def __init__(self, message: str, *, code: str, path: Path, points: Iterable[int] = ()) -> None:
        super().__init__(message, code=code, points=points)
        self.path = path


class StalePlanError(XedaError):
    """A plan no longer matches the files it was made from. `difference` says what changed."""

    def __init__(self, message: str, *, code: str, difference: str) -> None:
        super().__init__(message, code=code)
        self.difference = difference


class RecordError(XedaError):
    """A record is missing, unreadable, of an unsupported schema, or not written."""

    def __init__(self, message: str, *, code: str, path: Path) -> None:
        super().__init__(message, code=code)
        self.path = path


class RunReplacedError(XedaError):
    """A later run replaced the run that a record describes."""

    def __init__(
        self,
        message: str,
        *,
        code: str,
        path: Path,
        expected_run_id: str,
        found_run_id: str,
    ) -> None:
        super().__init__(message, code=code)
        self.path = path
        self.expected_run_id = expected_run_id
        self.found_run_id = found_run_id


class RemoteError(XedaError):
    """The remote host cannot be reached or set up. `host` names it."""

    def __init__(self, message: str, *, code: str, host: str) -> None:
        super().__init__(message, code=code)
        self.host = host


class RunFailedError(XedaError):
    """A launch did not pass. `result` holds the outcome of every node."""

    def __init__(self, message: str, *, code: str, result: Any) -> None:
        super().__init__(message, code=code)
        self.result = result


class LeaseHeldError(XedaError):
    """The run directory or the storage at `path` is held by the calling thread."""

    def __init__(self, message: str, *, code: str, path: Path) -> None:
        super().__init__(message, code=code)
        self.path = path
