"""The suite's own isolation (D21): a test works under `tmp_path`, never in the checkout."""

from pathlib import Path

import pytest

from .tool_utils import _opted_in

CHECKOUT = Path(__file__).parent.parent

#: The opt-in layers (Vivado, Docker, external Bluespec repositories) work in the checkout's
#: `xeda_run/`, which a container can mount where the system temporary directory is not
#: (`tool_utils.checkout_work_dir`, `test_bsc_external`): the one entry they may add.
OPT_IN_LAYERS = ("XEDA_TESTS_VIVADO", "XEDA_TESTS_DOCKER", "XEDA_TESTS_EXTERNAL")
OPT_IN_WORK_DIR = "xeda_run"
#: The suite's own bytecode cache, written as pytest imports the test modules: the one
#: `__pycache__` it may add. Nothing xeda starts may add one elsewhere (a cocotb testbench's is
#: cached in the run directory).
TESTS_PYCACHE = CHECKOUT / "tests" / "__pycache__"


def _watched() -> list[Path]:
    """The checkout's top level, `tests/`, and every directory holding an example design."""
    examples = {
        p.parent
        for p in (CHECKOUT / "examples").rglob("*")
        if p.suffix in (".toml", ".yaml", ".yml") and "xeda_run" not in p.parts
    }
    return [CHECKOUT, CHECKOUT / "tests", *sorted(examples)]


def _entries(directories: list[Path]) -> set[str]:
    return {
        str(path.relative_to(CHECKOUT))
        for directory in directories
        for path in directory.iterdir()
        if not path.name.startswith(".") and path != TESTS_PYCACHE
    }


@pytest.fixture(scope="session", autouse=True)
def _nothing_is_written_into_the_checkout():
    directories = _watched()
    before = _entries(directories)
    yield
    new = _entries(directories) - before
    if any(_opted_in(layer) for layer in OPT_IN_LAYERS):
        new.discard(OPT_IN_WORK_DIR)
    assert not new, f"tests wrote into the checkout: {sorted(new)}"
