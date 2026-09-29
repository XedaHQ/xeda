"""open_xc7's artifacts, with its tools stubbed: nextpnr-xilinx and prjxray's two packers
(`fasm2frames`, `xc7frames2bit`) write the files they are asked for, or leave some out. These tests
check what the flow records from the files it finds, not what the real toolchain produces."""

from pathlib import Path

import pytest

import xeda.flows.openxc7 as openxc7
from xeda import Design
from xeda.flow import FPGA
from xeda.flows import OpenXC7, YosysFpga

DESIGN0 = Path(__file__).parent / "resources/design0/design0.toml"
PART = "xc7a35tcpg236-1"


def _prjxray_db(root: Path) -> Path:
    db = root / "db"
    (db / "artix7" / PART).mkdir(parents=True)
    (db / "artix7" / PART / "part.yaml").write_text("part: test\n")
    return db


def _stub_packers(monkeypatch, write_bitstream: bool = True) -> list:
    """Stub prjxray's packers, which open_xc7 runs through `run_process`: `fasm2frames` writes
    the frames, `xc7frames2bit` the bitstream unless `write_bitstream` is false."""
    calls = []

    def fake_run_process(executable, args, **kwargs):
        calls.append(executable)
        if executable == "fasm2frames":
            Path(args[-1]).write_text("frames\n")
        elif write_bitstream:
            output = Path(args[args.index("--output_file") + 1])
            output.parent.mkdir(parents=True, exist_ok=True)
            output.write_bytes(b"bitstream")

    monkeypatch.setattr(openxc7, "run_process", fake_run_process)
    return calls


class FakeNextpnr:
    """nextpnr-xilinx, stubbed: writes each file it is asked for (`--fasm=...`) but those of
    the flags in `skip`."""

    def __init__(self, skip=()) -> None:
        self.skip = set(skip)
        self.runs: list = []

    def executable_path(self) -> str:
        return "/stub/nextpnr-xilinx"

    def run(self, *args) -> None:
        self.runs.append(args)
        for arg in map(str, args):
            flag, _, value = arg.partition("=")
            if flag in ("--fasm", "--write", "--log") and flag not in self.skip:
                Path(value).write_text(flag)


def _openxc7(tmp_path: Path, monkeypatch, **settings) -> OpenXC7:
    """An open_xc7 flow in `tmp_path/run`, as the runner would call its `run()`: its yosys
    dependency completed (only its netlist file exists), the chip database a file."""
    design = Design.from_toml(DESIGN0)
    yosys = YosysFpga(YosysFpga.Settings(fpga=FPGA(PART)), design, tmp_path / "yosys")
    yosys.run_path.mkdir()
    (yosys.run_path / "netlist.json").write_text("{}")
    chipdb = tmp_path / f"{PART}.bin"
    chipdb.write_bytes(b"")
    run_path = tmp_path / "run"
    run_path.mkdir()
    monkeypatch.chdir(run_path)
    flow = OpenXC7(
        OpenXC7.Settings(
            fpga=FPGA(PART), prjxray_db_dir=_prjxray_db(tmp_path), chipdb=chipdb, **settings
        ),
        design,
        run_path,
    )
    flow.init()
    flow.completed_dependencies.append(yosys)
    return flow


def test_openxc7_records_generated_bitstream(tmp_path: Path, monkeypatch) -> None:
    """The frames and the bitstream the (stubbed) packers write are recorded."""
    fasm = tmp_path / "top.fasm"
    fasm.write_text("test\n")
    bitstream = tmp_path / "outputs" / "top.bit"
    calls = _stub_packers(monkeypatch)
    design = Design.from_toml(DESIGN0)
    flow = OpenXC7(
        OpenXC7.Settings(
            fpga=FPGA(PART),
            prjxray_db_dir=_prjxray_db(tmp_path),
            bitstream=bitstream,
        ),
        design,
        tmp_path / "run",
    )

    produced = flow.generate_bitstream(fasm)

    assert calls == ["fasm2frames", "xc7frames2bit"]
    assert produced == bitstream.resolve()
    assert flow.artifacts["frames"] == fasm.with_suffix(".frames").resolve()
    assert flow.artifacts["bitstream"] == bitstream.resolve()


@pytest.mark.parametrize("skip", [(), ("--write", "--log")], ids=["all", "fasm_only"])
def test_openxc7_run_records_what_nextpnr_wrote(skip, tmp_path: Path, monkeypatch) -> None:
    """`run()` records nextpnr's FASM, JSON netlist and log, and the frames and bitstream packed
    from the FASM -- each only if the (stubbed) tool wrote it."""
    monkeypatch.setattr(OpenXC7, "next_pnr", FakeNextpnr(skip))
    _stub_packers(monkeypatch)
    flow = _openxc7(tmp_path, monkeypatch, json_output="routed.json")

    flow.run()

    run_path = flow.run_path.resolve()
    expected = {
        "json_netlist": run_path / "routed.json",
        "nextpnr_log": run_path / "nextpnr.log",
        "fasm": run_path / "design0.fasm",
        "frames": run_path / "design0.frames",
        "bitstream": run_path / "design0.bit",
    }
    if skip:
        del expected["json_netlist"], expected["nextpnr_log"]
    assert flow.artifacts == expected
    assert all(path.is_file() for path in expected.values())


def test_openxc7_records_no_bitstream_the_packer_did_not_write(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(OpenXC7, "next_pnr", FakeNextpnr())
    _stub_packers(monkeypatch, write_bitstream=False)
    flow = _openxc7(tmp_path, monkeypatch)

    flow.run()

    assert "frames" in flow.artifacts
    assert "bitstream" not in flow.artifacts
