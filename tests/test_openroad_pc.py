"""PCD9: OpenROAD prepares no output until run(), and keeps its own cell policy."""

import pytest

from xeda import Design
from xeda.flows import Openroad
from xeda.utils import WorkingDirectory


@pytest.mark.parametrize("copy_platform_files", [False, True])
def test_init_and_prepare_inputs_write_nothing(tmp_path, copy_platform_files):
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "d"})
    run = tmp_path / "run"
    run.mkdir()
    flow = Openroad(
        {"platform": "nangate45", "copy_platform_files": copy_platform_files}, design, run
    )
    with WorkingDirectory(run):
        flow.init()
        flow.prepare_inputs()
    assert list(run.iterdir()) == []
