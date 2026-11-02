"""Where the implementation of Xeda lives: the one place that knows the layout of the package.

`IMPL` is the dotted name of the package that holds the implementation. Every module path that
the code builds as text (`impl_module`) and every directory that it reads files from
(`impl_root`, `package_root`, `source_root`) comes from here, so a change of the layout changes
this module and the places of its files, and nothing else. This module imports no other module of
Xeda and stays at the top of the implementation package.
"""

from pathlib import Path
from typing import Final

#: The package that holds the implementation.
IMPL: Final[str] = "xeda"

#: How many packages `IMPL` has below the public package `xeda`.
_DEPTH: Final[int] = IMPL.count(".")


def impl_module(name: str) -> str:
    """Return the dotted name of the module `name` (for example `flows` or `remote.agent`) of the
    implementation."""
    return f"{IMPL}.{name}"


def impl_root() -> Path:
    """Return the directory of the implementation package. A location that a run counts as
    `$XEDA/...` is relative to it."""
    return Path(__file__).absolute().parent


def package_root() -> Path:
    """Return the directory of the public package `xeda`: the files that the installed package
    holds, and the files that a remote host receives."""
    root = impl_root()
    return root.parents[_DEPTH - 1] if _DEPTH else root


def source_root() -> Path:
    """Return the directory that holds the package `xeda`: what a remote code snapshot puts on
    `PYTHONPATH`."""
    return package_root().parent


def schemas_dir() -> Path:
    """Return the directory of the JSON Schemas that the package ships. The schemas are public
    files: they stay under `xeda/data/schemas`, wherever the implementation is."""
    return package_root() / "data" / "schemas"
