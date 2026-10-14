"""The `tiny` test platform as a PDK store, for the tests that run a flow on a platform.

`resources/platforms/tiny/` holds the files of a small standard cell library, and
`resources/platforms/tiny.yaml` is the residue file that names them. `tiny_store` copies the files
into a store of the provider `directory`, under a test directory, so that a test can change them.
A flow that gets `tiny_settings` works on the platform without a bundled platform or a PDK in the
home directory.
"""

import shutil
from pathlib import Path
from typing import Any

TINY = Path(__file__).parent / "resources" / "platforms" / "tiny"
TINY_RESIDUE = Path(__file__).parent / "resources" / "platforms" / "tiny.yaml"
#: The same platform with the values that the OpenROAD flows read (the power grid, the wire RC).
TINY_OPENROAD_RESIDUE = Path(__file__).parent / "resources" / "platforms" / "tiny_openroad.yaml"


def tiny_store(root: Path, name: str = "store") -> Path:
    """Copy the files of the `tiny` platform to `<root>/<name>/tiny`; return the store."""
    store = root / name
    shutil.copytree(TINY, store / "tiny")
    return store


def tiny_settings(store: Path, *, residue: Path = TINY_RESIDUE, **given: Any) -> dict[str, Any]:
    """Return the settings that select the `tiny` platform in `store`, with `given` added.

    `residue` is the residue file of the platform: `TINY_OPENROAD_RESIDUE` for a flow that reads
    the values of OpenROAD."""
    return {"platform_file": str(residue), "pdk_root": str(store), **given}
