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
