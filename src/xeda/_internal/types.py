"""Shared type definitions."""

import enum
import os
from pathlib import Path
from typing import Union

PathLike = Union[str, Path, os.PathLike]


class Unset(enum.Enum):
    """The type of `UNSET`, the one value that means "the caller did not give this argument".

    `None` is a value that a caller may give on purpose (no target), so it cannot also mean
    "not given". Annotate a parameter that takes `UNSET` as `Literal[Unset.UNSET]`."""

    UNSET = "UNSET"

    def __repr__(self) -> str:
        return "UNSET"


UNSET = Unset.UNSET


class Question(str):
    """The text of a question that a launch asks `Workspace.confirm`, and what it is about.

    It is a `str`, so a callback that only shows the text needs nothing else. `removal` is true
    when a yes removes run directories. `undone` says, for a log, what stays as it is when nobody
    says yes, such as `Not removing 2 run directories`. Both are read-only."""

    removal: bool
    undone: str

    def __new__(cls, text: str, *, removal: bool, undone: str) -> "Question":
        question = super().__new__(cls, text)
        object.__setattr__(question, "removal", removal)
        object.__setattr__(question, "undone", undone)
        return question

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError(f"a Question is read-only: cannot set `{name}`")

    def __delattr__(self, name: str) -> None:
        raise AttributeError(f"a Question is read-only: cannot delete `{name}`")

    def __getnewargs_ex__(self) -> tuple[tuple[str], dict[str, object]]:
        """Let a copy or a pickle keep `removal` and `undone`."""
        return (str(self),), {"removal": self.removal, "undone": self.undone}
