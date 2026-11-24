"""A link on the way to a header that a C source of `bsc_sim` includes is an input by itself.

The test makes links, so it runs in the macOS job too; it needs bsc and a C compiler."""

from tests.test_bsc import (
    BscSim,
    DefaultRunner,
    _bdpi_headers_design,
    _disposition,
    require_bluesim,
    require_c_toolchain,
)


def test_a_header_link_that_leads_to_another_file_runs_the_simulation_again(tmp_path):
    """`inner/scale.h` is a link to `scale_3.h`. A link that leads to `scale_3.h` and later to
    `scale_3_copy.h`, which both exist, is a change, whatever the old target holds."""
    require_bluesim()
    require_c_toolchain()
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    design = _bdpi_headers_design(tmp_path / "d")
    root = tmp_path / "d"
    (root / "inner" / "scale_3.h").write_text("#define SCALE 3\n")
    (root / "inner" / "scale_3_copy.h").write_text("#define SCALE 3\n")
    link = root / "inner" / "scale.h"
    link.unlink()
    link.symlink_to("scale_3.h")
    assert _disposition(runner, BscSim, design, {}) == "ran"
    assert _disposition(runner, BscSim, design, {}) == "reused"
    link.unlink()
    link.symlink_to("scale_3_copy.h")
    assert _disposition(runner, BscSim, design, {}) == "ran"
    assert _disposition(runner, BscSim, design, {}) == "reused"
