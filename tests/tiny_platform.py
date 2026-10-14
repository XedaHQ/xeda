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


def tiny_store(root: Path, name: str = "store") -> Path:
    """Copy the files of the `tiny` platform to `<root>/<name>/tiny`; return the store."""
    store = root / name
    shutil.copytree(TINY, store / "tiny")
    return store


def tiny_settings(store: Path, **given: Any) -> dict[str, Any]:
    """Return the settings that select the `tiny` platform in `store`, with `given` added."""
    return {"platform_file": str(TINY_RESIDUE), "pdk_root": str(store), **given}
