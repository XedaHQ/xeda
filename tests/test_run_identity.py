"""What identifies a flow run: `flowrun_hash` over the settings, beside the design's hash.

A run's identity depends on what its inputs mean, not on where anything is: moving a design,
copying it, or starting xeda from another directory keeps it. Design sources count by content
(`Design.rtl_hash`); settings count by value, with a path counting as its text relative to the
design root or start directory -- no file or directory named in the settings is ever read.
"""

import json
import os
import shutil
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow import flowrun_hash
from xeda.flow_runner import DefaultRunner
from xeda.flows import VivadoSynth

TESTS_DIR = Path(__file__).parent.absolute()
SQRT = TESTS_DIR.parent / "examples" / "vhdl" / "sqrt"


def _design_copy(where: Path) -> Path:
    where.mkdir(parents=True)
    shutil.copy(SQRT / "sqrt.vhdl", where)
    (where / "c.xdc").write_text("create_clock -period 5 [get_ports clk]\n")
    return where


def _hash(design_root: Path, runner_cwd: Path, **settings) -> str:
    validated = VivadoSynth.Settings.from_input(
        {"fpga": "xc7a100tftg256-2L", **settings}, design_root=design_root, runner_cwd=runner_cwd
    )
    return flowrun_hash("vivado_synth", validated)


def _design(root: Path) -> Design:
    return Design(
        name="sqrt",
        rtl={"sources": ["sqrt.vhdl"], "top": "sqrt", "clock_port": "clk"},
        design_root=root,
    )


def test_the_directory_xeda_is_started_from_does_not_change_the_hash(tmp_path):
    design = _design_copy(tmp_path / "d")
    first, second = tmp_path / "a", tmp_path / "b"
    assert _hash(design, first, tcl_files=[first / "hook.tcl"]) == _hash(
        design, second, tcl_files=[second / "hook.tcl"]
    )


@pytest.mark.parametrize(
    "xdc",
    ["c.xdc", "$DESIGN_ROOT/c.xdc", "{root}/c.xdc"],
    ids=["relative", "design-root-variable", "absolute-under-root"],
)
def test_an_identical_copy_of_the_design_elsewhere_hashes_the_same(tmp_path, xdc):
    one, other = _design_copy(tmp_path / "one"), _design_copy(tmp_path / "elsewhere" / "other")

    here = _hash(one, tmp_path, xdc_files=[xdc.format(root=one)])
    there = _hash(other, tmp_path, xdc_files=[xdc.format(root=other)])

    assert here == there
    assert _design(one).rtl_hash == _design(other).rtl_hash


def test_a_different_setting_is_a_different_run(tmp_path):
    design = _design_copy(tmp_path / "d")
    assert _hash(design, tmp_path, clock_period=5.0) != _hash(design, tmp_path, clock_period=4.0)
    assert _hash(design, tmp_path, xdc_files=["c.xdc"]) != _hash(design, tmp_path)


def test_design_sources_count_by_content_and_settings_paths_by_text(tmp_path):
    """Editing an RTL source is a different run; editing a file a setting names is not -- a
    file whose content should matter belongs in the design's sources (`SourceType.Xdc`, ...)."""
    root = _design_copy(tmp_path / "d")
    rtl, settings = _design(root).rtl_hash, _hash(root, tmp_path, xdc_files=["c.xdc"])

    (root / "c.xdc").write_text("create_clock -period 2 [get_ports clk]\n")
    assert _hash(root, tmp_path, xdc_files=["c.xdc"]) == settings

    with open(root / "sqrt.vhdl", "a") as f:
        f.write("\n-- edited\n")
    assert _design(root).rtl_hash != rtl


def test_design_source_order_and_compile_metadata_are_part_of_the_hash(tmp_path):
    root = tmp_path / "d"
    root.mkdir()
    for name in ("a.vhd", "b.vhd"):
        (root / name).write_text(f"-- {name}\n")

    def design(sources, **rtl):
        return Design(
            name="d",
            design_root=root,
            rtl={"sources": sources, "top": "top", **rtl},
        )

    original = design(["a.vhd", "b.vhd"])
    assert original.rtl_hash != design(["b.vhd", "a.vhd"]).rtl_hash
    assert original.rtl_hash != design([{"file": "a.vhd", "standard": "2008"}, "b.vhd"]).rtl_hash


def test_design_source_relative_path_is_part_of_the_hash(tmp_path):
    """Source layout affects include lookup, while an identical copied layout remains portable."""
    one = tmp_path / "one"
    other = tmp_path / "other"
    for root in (one, other):
        (root / "rtl").mkdir(parents=True)
        (root / "vendor").mkdir()
        (root / "rtl" / "top.v").write_text("module top; endmodule\n")
        (root / "vendor" / "top.v").write_text("module top; endmodule\n")

    def source_hash(root, source):
        return Design(
            name="d",
            design_root=root,
            rtl={"sources": [source], "top": "top"},
        ).rtl_hash

    assert source_hash(one, "rtl/top.v") != source_hash(one, "vendor/top.v")
    assert source_hash(one, "rtl/top.v") == source_hash(other, "rtl/top.v")


def test_behavior_affecting_design_metadata_is_part_of_the_hash(tmp_path):
    root = tmp_path / "d"
    root.mkdir()
    (root / "top.v").write_text("module top(input clk); endmodule\n")

    def design(**rtl):
        return Design(
            name="d",
            design_root=root,
            rtl={"sources": ["top.v"], "top": "top", **rtl},
        )

    original = design(clock_port="clk")
    assert original.rtl_hash != design(clock_port="other_clk").rtl_hash
    assert original.rtl_hash != design(attributes={"keep": {"top": True}}).rtl_hash
    assert (
        original.rtl_hash
        != Design(
            name="d",
            design_root=root,
            rtl={"sources": ["top.v"], "top": "top", "clock_port": "clk"},
            hdl={"verilog": "2005"},
        ).rtl_hash
    )


def test_where_settings_were_given_is_not_part_of_them(tmp_path):
    design = _design_copy(tmp_path / "d")
    settings = VivadoSynth.Settings.from_input(
        {"fpga": "xc7a100tftg256-2L"}, design_root=design, runner_cwd=tmp_path
    )

    dumped = str(settings.model_dump())
    assert str(design) not in dumped and str(tmp_path) not in dumped
    assert settings.context == {"design_root": design, "runner_cwd": tmp_path}


@pytest.fixture
def fake_tools(monkeypatch):
    monkeypatch.setenv("PATH", str(TESTS_DIR / "fake_tools") + os.pathsep + os.environ["PATH"])


def test_an_unchanged_rerun_reuses_the_previous_run_from_any_directory(
    tmp_path, monkeypatch, fake_tools
):
    """A run whose inputs did not change is reused, not repeated -- also when xeda is started
    from another directory, which used to change the settings' hash."""
    design = Design.from_toml(SQRT / "sqrt.toml")
    settings = {"fpga": "xc7a12tcsg325-1", "clock_period": 5.5}
    runner = DefaultRunner(
        tmp_path / "xeda_run", cached_dependencies=True, skip_if_previous_run_exists=True
    )

    monkeypatch.chdir(_design_copy(tmp_path / "start_one"))
    first = runner.run_flow(VivadoSynth, design, dict(settings))
    monkeypatch.chdir(_design_copy(tmp_path / "start_two"))
    second = runner.run_flow(VivadoSynth, design, dict(settings))

    assert first is not None and second is not None and first.succeeded
    assert second.results.timestamp == first.results.timestamp, "the second run was repeated"
    changed = runner.run_flow(VivadoSynth, design, {**settings, "clock_period": 4.5})
    assert changed is not None and changed.results.timestamp != first.results.timestamp


def test_a_run_never_modifies_its_input_settings(tmp_path, fake_tools):
    """The flow completes a copy of its own; the input it was launched with -- what identifies
    and records the run -- is never modified, whatever the flow does to its copy."""
    design = Design.from_toml(SQRT / "sqrt.toml")
    given = VivadoSynth.Settings.from_input(
        {"fpga": "xc7a12tcsg325-1", "clock_period": 5.5, "bitstream": "sqrt.bit"},
        design_root=design.root_path,
    )
    before = given.model_dump()

    flow = DefaultRunner(tmp_path / "xeda_run").run_flow(VivadoSynth, design, given)

    assert flow is not None and flow.succeeded
    assert flow.settings.bitstream is not None and flow.settings.bitstream.is_absolute()
    assert given.model_dump() == before, "the flow's work reached the settings it was given"
    recorded = json.loads((flow.run_path / "settings.json").read_text())
    assert recorded["flow_settings"]["bitstream"] == "sqrt.bit"
    assert "effective_flow_settings" in recorded  # the flow's final settings, after `run()`


def test_a_recorded_settings_json_is_a_rerunnable_input(tmp_path, fake_tools):
    design = Design.from_toml(SQRT / "sqrt.toml")
    flow = DefaultRunner(tmp_path / "xeda_run").run_flow(
        VivadoSynth, design, {"fpga": "xc7a12tcsg325-1", "clock_period": 5.5}
    )
    assert flow is not None
    recorded = json.loads((flow.run_path / "settings.json").read_text())

    again = VivadoSynth.Settings.from_input(recorded["flow_settings"], design_root=design.root_path)

    assert flowrun_hash("vivado_synth", again) == recorded["flowrun_hash"]
