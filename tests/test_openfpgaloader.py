"""Packing and programmer command construction without touching real hardware."""

from pathlib import Path

import pytest

from xeda import Design
from xeda.flow import FlowFatalError
from xeda.flows import Nextpnr, Openfpgaloader
from xeda.tool import Tool


@pytest.mark.parametrize(
    "fpga,config,packer,extension",
    [
        (
            {"family": "ecp5", "vendor": "lattice", "capacity": "25k"},
            "config.txt",
            "ecppack",
            ".bit",
        ),
        (
            {"family": "ice40", "vendor": "lattice", "device": "ice40HX1K"},
            "config.asc",
            "icepack",
            ".bin",
        ),
    ],
)
def test_packs_before_programming(tmp_path, monkeypatch, fpga, config, packer, extension):
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "d"})
    settings = Openfpgaloader.Settings(
        fpga=fpga,
        cable="ft2232",
        write_flash=True,
        verify=True,
        freq=6000000,
        extra_args=["--scan-usb"],
    )
    flow = Openfpgaloader(settings, design, tmp_path / "loader")
    flow.init()
    pnr = Nextpnr(
        settings.nextpnr.model_copy(update={"fpga": settings.fpga}), design, tmp_path / "pnr"
    )
    pnr.run_path.mkdir()
    (pnr.run_path / config).write_text("config")
    flow.completed_dependencies.append(pnr)
    calls = []

    def fake_run(self, *args):
        calls.append((self.executable, args))
        if self.executable == packer:
            Path(args[1]).write_bytes(b"packed")

    monkeypatch.setattr(Tool, "run", fake_run)
    flow.run()
    assert [name for name, _ in calls] == [packer, "openFPGALoader"]
    output = Path(calls[0][1][1])
    assert output.suffix == extension
    assert calls[1][1][:2] == ("--bitstream", output)
    assert {"--cable", "--write-flash", "--verify", "--freq", "--scan-usb"} <= set(calls[1][1])
    assert flow.artifacts["bitstream"] == output


def test_unsupported_target_never_programs(tmp_path, monkeypatch):
    design = Design(name="d", rtl={"sources": [], "top": "d"})
    settings = Openfpgaloader.Settings(
        fpga={"family": "nexus", "vendor": "lattice", "device": "LIFCL-40"}
    )
    flow = Openfpgaloader(settings, design, tmp_path)
    flow.init()
    monkeypatch.setattr(Tool, "run", lambda self, *args: pytest.fail("must not program"))
    with pytest.raises(FlowFatalError, match="no verified bitstream packer"):
        flow.run()


def test_missing_packer_output_never_programs(tmp_path, monkeypatch):
    design = Design(name="d", design_root=tmp_path, rtl={"sources": [], "top": "d"})
    settings = Openfpgaloader.Settings(fpga={"family": "ecp5", "capacity": "25k"})
    flow = Openfpgaloader(settings, design, tmp_path / "loader")
    flow.init()
    pnr = Nextpnr(
        settings.nextpnr.model_copy(update={"fpga": settings.fpga}), design, tmp_path / "pnr"
    )
    pnr.run_path.mkdir()
    (pnr.run_path / "config.txt").write_text("config")
    flow.completed_dependencies.append(pnr)
    calls = []
    monkeypatch.setattr(Tool, "run", lambda self, *args: calls.append(self.executable))
    with pytest.raises(FlowFatalError, match="did not write"):
        flow.run()
    assert calls == ["ecppack"]
