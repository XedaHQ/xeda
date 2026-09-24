import re
import tempfile
from pathlib import Path

import pytest

from xeda import Design
from xeda.design import VhdlSettings
from xeda.flow import FlowException
from xeda.flow_runner import DefaultRunner
from xeda.flows import GhdlSim, GhdlSynth
from xeda.flows.ghdl import GhdlTool

from .tool_utils import require_ghdl

TESTS_DIR = Path(__file__).parent.absolute()
EXAMPLES_DIR = TESTS_DIR.parent / "examples"

debug = False


def test_ghdl_sim_py() -> None:
    require_ghdl()
    # settings = dict(fpga=FPGA("xc7a12tcsg325-1"), clock_period=5.5)
    # run_dir = "tests_run_dir"
    design_paths = [
        EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.toml",
        EXAMPLES_DIR / "vhdl" / "Trivium" / "trivium.xeda.yaml",
        EXAMPLES_DIR / "vhdl" / "pipeline" / "pipelined_adder.toml",
    ]
    with tempfile.TemporaryDirectory(dir=Path.cwd()) as run_dir:
        print("Xeda run dir: ", run_dir)
        for design in design_paths:
            xeda_runner = DefaultRunner(run_dir, debug=debug)
            flow = xeda_runner.run(GhdlSim, design, flow_overrides=dict(debug=debug, verbose=debug))
            assert flow is not None, "run_flow returned None"
            settings_json = flow.run_path / "settings.json"
            results_json = flow.run_path / "results.json"
            assert settings_json.exists()
            assert flow.succeeded
            assert isinstance(flow.settings, GhdlSim.Settings)
            assert results_json.exists()


@pytest.mark.parametrize(
    "lib_paths, flags",
    [
        ([], []),
        ([(None, "/libs/a")], ["-P/libs/a"]),
        ([("mylib", "/libs/b"), (None, "/libs/c")], ["-P/libs/b", "-P/libs/c"]),
        ([("ieee_proposed", None)], []),
    ],
)
def test_lib_paths_become_ghdl_search_directories(lib_paths, flags):
    """`lib_paths` entries are (library name, path) pairs; GHDL's `-P<dir>` takes the path. The
    whole pair used to be formatted in, as `-P('mylib', '/libs/b')`."""
    ss = GhdlSim.Settings(lib_paths=lib_paths)
    assert [f for f in ss.common_flags(VhdlSettings()) if f.startswith("-P")] == flags


def _vhdl_inverter(entity: str) -> str:
    """Create a small VHDL inverter design."""
    return (
        "library ieee; use ieee.std_logic_1164.all;\n"
        f"entity {entity} is port(a: in std_logic; y: out std_logic); end;\n"
        f"architecture rtl of {entity} is begin y <= not a; end;\n"
    )


def _same_stem_design(root: Path, top: bool = False) -> Design:
    """`rtl/a/fifo.vhd` and `rtl/b/fifo.vhd`: distinct files sharing a base name, which GHDL's
    LLVM and GCC backends compile to the same object file (`fifo.o`)."""
    sources = []
    for sub in ("a", "b"):
        path = root / "rtl" / sub / "fifo.vhd"
        path.parent.mkdir(parents=True)
        path.write_text(_vhdl_inverter(f"fifo_{sub}"))
        sources.append(f"rtl/{sub}/fifo.vhd")
    if top:
        (root / "top.vhd").write_text(
            "library ieee; use ieee.std_logic_1164.all;\n"
            "entity top is port(a: in std_logic; y1, y2: out std_logic); end;\n"
            "architecture rtl of top is begin\n"
            "  u1: entity work.fifo_a port map(a, y1);\n"
            "  u2: entity work.fifo_b port map(a, y2);\n"
            "end;\n"
        )
        sources.append("top.vhd")
    return Design(
        name="stem",
        design_root=root,
        rtl={"sources": sources, "top": "top" if top else "fifo_b"},
    )


def _modules(verilog: Path) -> list:
    """Return the VHDL modules used by GHDL tests."""
    return re.findall(r"^module\s+(\w+)", verilog.read_text(), re.M)


@pytest.fixture
def ghdl_commands(monkeypatch):
    """The ghdl subcommands a flow runs (`analyze`, `synth`, ...), in order."""
    commands = []
    original_run = GhdlTool.run

    def recording_run(self, *args, **kwargs):
        commands.append(args[0] if args else None)
        return original_run(self, *args, **kwargs)

    monkeypatch.setattr(GhdlTool, "run", recording_run)
    return commands


def test_ghdl_synth_writes_one_verilog_file_per_same_stem_source(tmp_path, ghdl_commands):
    """Each VHDL source becomes its own Verilog file, named after its path, so two sources
    sharing a stem do not overwrite each other. `ghdl synth` elaborates by itself: running
    `ghdl make` first made the LLVM/GCC backends reject the two sources ("both compiled to
    'fifo.o'") before the per-source conversion ever ran."""
    require_ghdl()
    design = _same_stem_design(tmp_path)
    flow = DefaultRunner(tmp_path / "xeda_run").run_flow(
        GhdlSynth, design, {"verilog_output": "vout"}
    )
    assert flow is not None and flow.succeeded
    generated = [flow.run_path / p for p in flow.artifacts.generated_verilog]
    assert [p.name for p in generated] == ["rtl_a_fifo.v", "rtl_b_fifo.v"]
    assert [_modules(p) for p in generated] == [["fifo_a"], ["fifo_b"]]
    assert "make" not in ghdl_commands


def test_ghdl_synth_single_output_needs_no_make(tmp_path, ghdl_commands):
    """The single-file output elaborates the analyzed library's top unit with `ghdl synth`
    itself, so it also works for sources an LLVM/GCC `ghdl make` would refuse to link."""
    require_ghdl()
    design = _same_stem_design(tmp_path, top=True)
    flow = DefaultRunner(tmp_path / "xeda_run").run_flow(
        GhdlSynth, design, {"verilog_output": "stem.v"}
    )
    assert flow is not None and flow.succeeded
    modules = _modules(flow.run_path / "stem.v")
    assert len(modules) == 3 and "top" in modules
    assert "make" not in ghdl_commands


def test_ghdl_synth_output_name_collision_is_reported_before_synthesis(
    tmp_path, monkeypatch, ghdl_commands
):
    """Two sources whose output names coincide are an error naming both, raised before any
    `ghdl synth` runs -- not a bare `assert` (gone under `python -O`) after the first file was
    already written."""
    require_ghdl()
    design = _same_stem_design(tmp_path)
    monkeypatch.setattr(
        Design, "source_artifact_name", lambda self, src, suffix="": "same" + suffix
    )
    with pytest.raises(FlowException) as excinfo:
        DefaultRunner(tmp_path / "xeda_run").run_flow(GhdlSynth, design, {"verilog_output": "vout"})
    message = str(excinfo.value)
    assert "rtl/a/fifo.vhd" in message and "rtl/b/fifo.vhd" in message
    assert "same.v" in message
    assert ghdl_commands and "synth" not in ghdl_commands


if __name__ == "__main__":
    test_ghdl_sim_py()
