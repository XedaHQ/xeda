"""Where the source files of Xeda are in the checkout: the one helper that tests and tools use.

`IMPL_DIR` is the directory of the implementation package (`xeda.package_paths.IMPL`). A test
that reads a source file, a template or a data file of the implementation builds its path from it.
"""

from pathlib import Path

from xeda.package_paths import IMPL

ROOT = Path(__file__).resolve().parents[1]
#: The directory of the public package `xeda`.
SRC_XEDA = ROOT / "src" / "xeda"
#: The directory of the implementation package.
IMPL_DIR = SRC_XEDA.joinpath(*IMPL.split(".")[1:])
#: The agent skill that the package ships.
AGENT_DIR = IMPL_DIR / "data" / "agent"
#: The JSON Schemas that the package ships. They stay at a public path.
SCHEMAS_DIR = SRC_XEDA / "data" / "schemas"
