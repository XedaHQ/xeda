"""Where the code of a Flow class lies.

Xeda tells that the code of a flow changed by the files that define it (`code_digest`). A class
defined in a notebook cell, in the interactive interpreter or by `exec` has no such file.
"""

from __future__ import annotations

import inspect
from collections.abc import Iterable
from pathlib import Path


def find_source_file(cls: type) -> tuple[Path | None, str]:
    """Return the file that defines `cls` and "", or None and why there is none on disk.

    A class defined in a notebook cell has a file name that is no file (`<ipython-input-1-...>`),
    one defined in the interactive interpreter has a module without a file, and one defined with
    `exec` can have a module that is not imported."""
    try:
        source = inspect.getsourcefile(cls)
    except (OSError, TypeError):
        source = None
    if source is None:
        return None, f"Python knows no file for its module {cls.__module__}"
    if not Path(source).is_file():
        return None, f"{source} is not a file"
    return Path(source).resolve(), ""


def class_label(cls: type) -> str:
    """Return the module and the qualified name of `cls`."""
    return f"{cls.__module__}.{cls.__qualname__}"


def without_source_file(classes: Iterable[type]) -> str | None:
    """Return why the code of `classes` cannot be told, or None if each one has a file.

    The reason names the first class that has no source file on disk."""
    for cls in classes:
        file, why = find_source_file(cls)
        if file is None:
            return (
                f"its class {class_label(cls)} has no source file on disk ({why}), so Xeda cannot "
                "tell whether its code changed"
            )
    return None
