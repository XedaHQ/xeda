"""What a trace records: the design's sources, files named by settings, dependency outputs,
depfile-reported files, and every output."""

from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar, List, Optional

import pytest
from pydantic import Field

from xeda import Design
from xeda.flow import Flow, registered_flows
from xeda.digest import package_files
from xeda.flow_runner.trace_inputs import (
    XEDA_PACKAGE,
    artifact_files,
    design_files,
    flow_code_digest,
    implicit_input_files,
    parse_depfile,
    setting_files,
    xeda_code_digest,
)
from xeda.listing import directory_files


@pytest.fixture(scope="module")
def hooked():
    class HookedToy(Flow):
        """A flow with a hook script setting and an output-path setting."""

        results_description: ClassVar[dict[str, str]] = {}

        class Settings(Flow.Settings):
            hook: Optional[Path] = Field(None, description="A script the tool reads.")
            netlist: Path = Field(Path("out.json"), description="Where the netlist goes.")
            hooks: List[Path] = Field([], description="More scripts.")

        def run(self) -> None:
            pass

    yield HookedToy
    for name in (HookedToy.name, HookedToy.__name__):
        registered_flows.pop(name, None)


def test_every_existing_file_a_setting_names_is_a_candidate_input(tmp_path, hooked):
    """Wherever it is -- the run directory too: whether a file is the run's own is decided by
    its origin when the trace is built, not by where it lies."""
    root, run_dir = tmp_path / "design", tmp_path / "run"
    root.mkdir()
    run_dir.mkdir()
    (root / "pre.tcl").write_text("puts hi\n")
    (root / "post.tcl").write_text("puts bye\n")
    (run_dir / "out.json").write_text("{}\n")
    settings = hooked.Settings.from_input(
        {
            "hook": "pre.tcl",
            "hooks": ["$DESIGN_ROOT/post.tcl", "missing.tcl"],
            "netlist": str(run_dir / "out.json"),
        },
        design_root=root,
        runner_cwd=tmp_path,
    )
    assert setting_files(settings) == [
        (root / "pre.tcl").resolve(),
        (run_dir / "out.json").resolve(),
        (root / "post.tcl").resolve(),
    ]


def test_a_relative_path_counts_under_both_the_design_root_and_the_start_directory(
    tmp_path, hooked
):
    """Which one a tool opens depends on where it runs: both are recorded, not the first."""
    root, start = tmp_path / "design", tmp_path / "start"
    root.mkdir()
    start.mkdir()
    (root / "pre.tcl").write_text("puts design\n")
    (start / "pre.tcl").write_text("puts start\n")
    settings = hooked.Settings.from_input({"hook": "pre.tcl"}, design_root=root, runner_cwd=start)
    assert setting_files(settings) == [(root / "pre.tcl").resolve(), (start / "pre.tcl").resolve()]


def test_lib_paths_only_the_path_half_of_each_tuple_is_a_setting_file(tmp_path, hooked):
    root = tmp_path / "design"
    root.mkdir()
    (root / "simprims_ver").write_text("a file that happens to share a library's name\n")
    (root / "unisim.lib").write_text("the actual library file\n")
    settings = hooked.Settings.from_input(
        {"lib_paths": [("simprims_ver", None), (None, "unisim.lib")]},
        design_root=root,
        runner_cwd=tmp_path,
    )
    assert setting_files(settings) == [(root / "unisim.lib").resolve()]


def test_design_files_are_every_rtl_and_testbench_source(tmp_path):
    (tmp_path / "a.v").write_text("module a; endmodule\n")
    (tmp_path / "tb.v").write_text("module tb; endmodule\n")
    design = Design(
        name="d",
        rtl={"sources": ["a.v"], "top": "a"},
        tb={"sources": ["tb.v"], "top": "tb"},
        design_root=tmp_path,
    )
    assert design_files(design) == [(tmp_path / "a.v").resolve(), (tmp_path / "tb.v").resolve()]


def test_a_depfile_lists_what_the_tool_read(tmp_path):
    depfile = tmp_path / "yosys.d"
    depfile.write_text("out.json: a.v inc/defs.vh \\\n  /abs/with\\ space.v\n")
    assert parse_depfile(depfile, tmp_path) == [
        tmp_path / "a.v",
        tmp_path / "inc/defs.vh",
        Path("/abs/with space.v"),
    ]


def test_implicit_input_files_are_what_depfiles_and_the_flow_name(tmp_path, hooked):
    """A depfile lists the rendered script the flow wrote into its own run directory alongside
    a header it read from outside, and the flow registers a file it read on its own: all are
    candidates; `build_trace` tells the run's own script apart by its origin."""
    root, run_dir = tmp_path / "design", tmp_path / "run"
    root.mkdir()
    run_dir.mkdir()
    (root / "a.v").write_text("module a; endmodule\n")
    outside = root / "inc.vh"
    outside.write_text("`define W 4\n")
    inside = run_dir / "script.ys"
    inside.write_text("read_verilog a.v\n")
    board = root / "board.lpf"
    board.write_text("LOCATE COMP clk SITE A1;\n")
    depfile = run_dir / "yosys.d"
    depfile.write_text(f"out.json: {inside} {outside}\n")
    design = Design(name="d", rtl={"sources": ["a.v"], "top": "a"}, design_root=root)
    settings = hooked.Settings.from_input({}, design_root=root, runner_cwd=tmp_path)
    flow = hooked(settings, design, run_path=run_dir)
    flow.depfiles = [depfile]
    flow.implicit_inputs = [board]
    assert implicit_input_files(flow) == sorted(
        [outside.resolve(), inside.resolve(), board.resolve()]
    )


def test_a_nested_model_s_files_are_candidates_but_not_a_dependency_s(tmp_path):
    """yosys's ghdl plugin settings are no dependency: the files they name are yosys's inputs.
    `vivado_synth` records its own files; its consumer records the declared hand-over."""
    from xeda.flows import VivadoPostsynthSim, VivadoSynth
    from xeda.flows.yosys import YosysFpga

    (tmp_path / "ghdl.lib").write_text("library\n")
    (tmp_path / "pins.xdc").write_text("# pins\n")
    yosys = YosysFpga.Settings.from_input(
        {"ghdl": {"lib_paths": [[None, "ghdl.lib"]]}},
        design_root=tmp_path,
        runner_cwd=tmp_path,
    )
    assert setting_files(yosys) == [(tmp_path / "ghdl.lib").resolve()]
    producer = VivadoSynth.Settings.from_input(
        {"fpga": "xc7a100t", "xdc_files": ["pins.xdc"]},
        design_root=tmp_path,
        runner_cwd=tmp_path,
    )
    depender = VivadoPostsynthSim.Settings.from_input({})
    assert setting_files(depender) == []
    assert setting_files(producer) == [(tmp_path / "pins.xdc").resolve()]


def test_a_file_valued_parameter_and_define_are_design_files(tmp_path):
    (tmp_path / "a.v").write_text("module a; endmodule\n")
    (tmp_path / "rom.mem").write_text("00\n")
    (tmp_path / "init.hex").write_text("ff\n")
    design = Design(
        name="d",
        rtl={
            "sources": ["a.v"],
            "top": "a",
            "parameters": {"ROM": {"file": "rom.mem"}, "W": 8, "NAME": "a.v"},
            "defines": {"INIT": str(tmp_path / "init.hex")},
        },
        design_root=tmp_path,
    )
    assert sorted(design_files(design)) == sorted(
        [(tmp_path / f).resolve() for f in ("a.v", "rom.mem", "init.hex")]
    )


def test_the_flow_code_digest_is_stable_and_names_the_flow(hooked):
    assert flow_code_digest(hooked) == flow_code_digest(hooked)
    assert len(flow_code_digest(hooked)) == 32


def test_the_xeda_code_digest_covers_helpers_templates_and_data():
    """An editable install keeps its version string across edits: a helper flows use (template
    filters in utils.py, cocotb.py, tool.py) or bundled board data must count as xeda's code."""
    files = {p.relative_to(XEDA_PACKAGE).as_posix() for p in package_files(XEDA_PACKAGE)}
    assert {"utils.py", "cocotb.py", "tool.py", "data/boards.toml"} <= files
    assert "flows/yosys/templates/yosys_synth.ys" in files
    assert not any("__pycache__" in f or f.endswith(".pyc") for f in files)
    assert xeda_code_digest() == xeda_code_digest() and len(xeda_code_digest()) == 32


def test_a_flow_of_xeda_s_own_has_no_separate_code_digest():
    from xeda.flows import YosysFpga

    assert flow_code_digest(YosysFpga) == ""


def test_one_walker_lists_every_file_under_a_directory():
    """R50 m: every "every file under" of the trace and of delivery is `listing.directory_files`
    (a link as itself unless asked to follow, no `rglob` that resolves or follows)."""
    import xeda

    package = Path(xeda.__file__).parent
    for module in ("flow_runner/trace.py", "flow_runner/trace_inputs.py", "deliver.py"):
        text = (package / module).read_text()
        assert "rglob(" not in text and "os.walk(" not in text, module


def test_a_followed_link_never_enters_a_pruned_directory(tmp_path):
    """Opus minor: pruned by (st_dev, st_ino), so a library link to an ancestor of the run root
    does not list the runs through it."""
    libs = tmp_path / "t" / "libs"
    libs.mkdir(parents=True)
    (libs / "a.v").write_text("")
    run_root = tmp_path / "t" / "xeda_run"
    (run_root / "d" / "f").mkdir(parents=True)
    (run_root / "d" / "f" / "out.v").write_text("")
    (libs / "up").symlink_to(tmp_path / "t", target_is_directory=True)
    listed = directory_files(libs, prune=[run_root], follow_links=True)
    assert libs.resolve() / "a.v" in listed
    assert not any("xeda_run" in p.parts for p in listed), listed


def test_an_artifact_directory_s_files_are_listed_without_following_a_link_out(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("x\n")
    run_path = tmp_path / "xeda_run" / "d" / "f"
    (run_path / "out").mkdir(parents=True)
    (run_path / "out" / "a.txt").write_text("a\n")
    (run_path / "out" / "elsewhere").symlink_to(outside, target_is_directory=True)
    flow = SimpleNamespace(results={"artifacts": {"out": "out"}}, artifacts={}, run_path=run_path)
    files = artifact_files(flow)  # type: ignore[arg-type]
    assert (run_path / "out" / "a.txt").resolve() in files
    assert not any(p.is_relative_to(outside.resolve()) for p in files)
