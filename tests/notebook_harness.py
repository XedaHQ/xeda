"""Execute a notebook in a kernel of the test environment.

The kernel starts in a given directory with a given environment. The kernel's connection file and
IPython directory are in a temporary directory of their own, so a run writes nothing in the home
directory of the user.
"""

import os
import tempfile
from collections.abc import Mapping
from pathlib import Path

import nbformat
from jupyter_client.manager import KernelManager
from nbclient import NotebookClient


def run_notebook(
    path: Path, *, cwd: Path, env: Mapping[str, str], timeout: int = 1800
) -> nbformat.NotebookNode:
    """Execute the notebook `path` with `cwd` as the working directory of the kernel and return it
    with its outputs. `env` adds to the environment of this process. A cell that raises an error
    raises `nbclient.exceptions.CellExecutionError`."""
    nb = nbformat.read(path, as_version=4)
    with tempfile.TemporaryDirectory(prefix="notebook-kernel-") as private:
        manager = KernelManager(
            kernel_name="python3", connection_file=str(Path(private) / "kernel.json")
        )
        client = NotebookClient(
            nb,
            timeout=timeout,
            kernel_name="python3",
            km=manager,
            resources={"metadata": {"path": str(cwd)}},
        )
        client.execute(env={**os.environ, **env, "IPYTHONDIR": str(Path(private) / "ipython")})
    return nb


def cell_output(nb: nbformat.NotebookNode, tag: str) -> str:
    """The text output of the cell of `nb` that has the tag `tag`, without the final newline.
    Raises `KeyError` when no cell has the tag."""
    for cell in nb.cells:
        if cell.cell_type == "code" and tag in cell.metadata.get("tags", ()):
            parts = [
                output.text if output.output_type == "stream" else output.data.get("text/plain", "")
                for output in cell.outputs
                if output.output_type in ("stream", "execute_result", "display_data")
            ]
            return "".join(parts).rstrip("\n")
    raise KeyError(tag)
