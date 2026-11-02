"""Where the source files of Xeda are in the checkout: the one helper that tests and tools use.

`IMPL_DIR` is the directory of the implementation package (`xeda.package_paths.IMPL`). A test
that reads a source file, a template or a data file of the implementation builds its path from it.
"""

import re
from pathlib import Path

from xeda.package_paths import IMPL, impl_module

ROOT = Path(__file__).resolve().parents[1]
#: The directory of the public package `xeda`.
SRC_XEDA = ROOT / "src" / "xeda"
#: The directory of the implementation package.
IMPL_DIR = SRC_XEDA.joinpath(*IMPL.split(".")[1:])
#: The implementation directory as the wheel lists it, relative to `src/`.
IMPL_PREFIX = "/".join(IMPL.split("."))
#: The agent skill that the package ships.
AGENT_DIR = IMPL_DIR / "data" / "agent"
#: The JSON Schemas that the package ships. They stay at a public path.
SCHEMAS_DIR = SRC_XEDA / "data" / "schemas"
#: The modules that the public surface keeps at their own path.
PUBLIC = ("errors", "introspect")


def internal_names() -> list[str]:
    """Return the names of the modules and packages that are not public: next to
    `xeda/__init__.py` and in the implementation package."""
    names = set()
    for directory in (SRC_XEDA, IMPL_DIR):
        for entry in directory.iterdir():
            name = entry.stem if entry.suffix == ".py" else entry.name
            if (entry.suffix == ".py" or (entry / "__init__.py").is_file()) and not (
                name.startswith("__") or name in PUBLIC
            ):
                names.add(name)
    return sorted(names)


def layout_script(text: str) -> str:
    """Return the Python source `text` for a subprocess with each module of the implementation
    (`xeda.flows`) at its real path. A script writes the module as `xeda.<module>`."""
    pattern = r"\bxeda\.(" + "|".join(internal_names()) + r")\b(?!\()"
    return re.sub(pattern, lambda match: impl_module(match.group(1)), text)
