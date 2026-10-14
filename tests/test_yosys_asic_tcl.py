"""The Tcl script of the liberty mapping has two modes that need the Tcl interface of yosys.

Hierarchical synthesis (`hierarchy.keep_min_cost`) keeps each module whose cost is above a number
of NAND2 gate equivalents. Operator wrapping (`arithmetic.wrap_operators` with `abc.mapper:
abc_new`) turns wide `$alu` and `$macc` cells into modules, one per architecture, with the
attributes that the place and route tool uses to swap architectures. `abc.mapper: abc_new` also
works without wrapping. The `.ys` script has none of them.

The rendering tests run the script under `tclsh` with every yosys command recording itself. The
tests at the end run a real yosys with Tcl (`XEDA_TESTS_YOSYS_TCL=1`, the `yosys` on `PATH`).
"""

import json
import re
import shutil
import subprocess
from importlib import resources
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda.flow import FlowSettingsError
from xeda.flow_runner import DefaultRunner
from xeda.flows import Yosys
from xeda.flows.yosys.yosys import ABC_NEW_SCRIPT, ABC_SPEED_GIA_ONLY

from .tiny_platform import TINY
from .test_yosys_asic import TINY_DONT_USE, _design, _scripted_launch, _tiny, _words
from .test_yosys_templates import TCL_HARNESS
from .tool_utils import require_tclsh, require_yosys_tcl

DATA = resources.files("xeda.flows.yosys").joinpath("data", "asic")
WRAP_SCRIPT = str(DATA.joinpath("synth_wrap_operators.tcl"))
BOOTH_MAP = str(DATA.joinpath("synth_wrap_operators-booth.v"))

#: The harness of the other Tcl tests, with the commands that the script defines or sources
#: replaced by stubs: `source` records itself and defines nothing, `convert_liberty_areas` records
#: itself instead of querying a design, and `design_has_extracted_operators` records itself and
#: answers `EXTRACTED`. `wrap_operators` is not defined, so it is recorded as any yosys command.
STUBS = """\
rename proc _real_proc
rename source _real_source
set ::extracted EXTRACTED
_real_proc proc {name arglist body} {
  if {$name eq "convert_liberty_areas"} {
    set body {lappend ::calls [list convert_liberty_areas]}
  }
  _real_proc $name $arglist $body
}
_real_proc source {args} {lappend ::calls [list source {*}$args]}
_real_proc design_has_extracted_operators {} {
  lappend ::calls [list design_has_extracted_operators]
  return $::extracted
}
_real_source [lindex $argv 0]
"""


def _calls(run_dir: Path, tmp_path: Path, extracted: bool = False) -> list[list[str]]:
    """The commands that yosys is handed by the Tcl script of `run_dir`, as their words."""
    require_tclsh()
    harness = tmp_path / "tcl_harness.tcl"
    harness.write_text(
        TCL_HARNESS.replace(
            "source [lindex $argv 0]", STUBS.replace("EXTRACTED", "1" if extracted else "0")
        )
    )
    script = run_dir / "yosys_synth.tcl"
    result = subprocess.run(
        ["tclsh", str(harness), str(script)], capture_output=True, text=True, timeout=60
    )
    assert result.returncode == 0, f"{result.stderr}\n{script.read_text()}"
    calls = [
        line.split("\x1f")[1:] for line in result.stdout.splitlines() if line.startswith("CALL\x1f")
    ]
    return [call for call in calls if call[0] not in ("log", "echo", "-import")]


def _render(tmp_path, monkeypatch, extracted: bool = False, **given: Any):
    """Launch `yosys` with a recording tool on the `tiny` platform; return its run directory and
    the commands of its Tcl script."""
    settings = _tiny(tmp_path, script_format="tcl", **given)
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, settings)
    return run_dir, _calls(run_dir, tmp_path, extracted)


def _liberty(settings: dict[str, Any]) -> str:
    return str(Path(settings["pdk_root"]) / "tiny" / "tiny.lib")


LIB_READ = ["read_liberty", "-overwrite", "-setattr", "liberty_cell"]
WHITEBOX_FLAGS = ["-unit_delay", "-wb", "-ignore_miss_func", "-ignore_buses"]
HIERARCHICAL = {"hierarchy": {"keep_min_cost": 5}}
WRAPPED = {"arithmetic": {"wrap_operators": True}, "abc": {"mapper": "abc_new"}}
ABC_NEW = {"abc": {"mapper": "abc_new"}}


def _segment(calls: list[list[str]], first: list[str], count: int) -> list[list[str]]:
    start = calls.index(first)
    return calls[start : start + count]


# ------------------------------------------------------------------------- the whitebox read


@pytest.mark.parametrize(
    "given", [HIERARCHICAL, ABC_NEW, WRAPPED], ids=["keep_min_cost", "abc_new", "wrapped"]
)
def test_the_liberty_files_are_also_read_as_whiteboxes_after_the_blackbox_read(
    tmp_path, monkeypatch, given
):
    """`keep_min_cost` takes the cost of the cells from their models, and `abc_new` reads their
    functions from them. Each file is read as a library cell first, then with its model."""
    settings = _tiny(tmp_path, script_format="tcl", **given)
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, settings)
    calls = _calls(run_dir, tmp_path)
    lib = _liberty(settings)
    assert _words(calls, "read_liberty") == [
        [*LIB_READ, "-lib", lib],
        [*LIB_READ, *WHITEBOX_FLAGS, lib],
    ]
    names = [call[0] for call in calls]
    assert max(i for i, name in enumerate(names) if name == "read_liberty") < names.index(
        "read_verilog"
    )


def test_every_liberty_file_is_read_as_a_whitebox_and_the_blackbox_reads_come_first(
    tmp_path, monkeypatch
):
    dff = tmp_path / "dff.lib"
    shutil.copy(TINY / "tiny.lib", dff)
    settings = _tiny(
        tmp_path, script_format="tcl", sequential={"dff_liberty": str(dff)}, **HIERARCHICAL
    )
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, settings)
    reads = _words(_calls(run_dir, tmp_path), "read_liberty")
    lib = _liberty(settings)
    assert reads == [
        [*LIB_READ, "-lib", lib],
        [*LIB_READ, "-lib", str(dff)],
        [*LIB_READ, *WHITEBOX_FLAGS, lib],
        [*LIB_READ, *WHITEBOX_FLAGS, str(dff)],
    ]


def test_without_either_mode_the_liberty_files_are_read_as_blackboxes_only(tmp_path, monkeypatch):
    settings = _tiny(tmp_path, script_format="tcl")
    calls = _calls(_scripted_launch(tmp_path, monkeypatch, Yosys, settings), tmp_path)
    assert _words(calls, "read_liberty") == [[*LIB_READ, "-lib", _liberty(settings)]]
    names = [call[0] for call in calls]
    assert not {"source", "convert_liberty_areas", "keep_hierarchy", "scratchpad", "abc_new"} & set(
        names
    )
    assert "abc" in names and ["flatten"] in calls


# ------------------------------------------------------------------------ hierarchical mode


def test_hierarchical_synthesis_keeps_the_modules_above_the_cost(tmp_path, monkeypatch):
    """The design is not flattened up front. `synth` stops before the fine part, the cost of each
    module comes from the cells of the library, the modules at or below the cost lose their
    hierarchy, and the second `synth` flattens what is left."""
    _, calls = _render(tmp_path, monkeypatch, **HIERARCHICAL)
    assert _segment(calls, ["proc"], 6) == [
        ["proc"],
        ["synth", "-run", ":fine"],
        ["convert_liberty_areas"],
        ["keep_hierarchy", "-min_cost", "5"],
        ["synth", "-flatten", "-run", "coarse:fine"],
        ["synth", "-top", "mac", "-run", "fine:", "-noabc"],
    ]
    assert ["flatten"] not in calls
    assert ["synth", "-flatten", "-run", ":fine"] not in calls


def test_hierarchical_synthesis_gives_the_extra_map_and_the_flags_to_both_coarse_runs(
    tmp_path, monkeypatch
):
    _, calls = _render(
        tmp_path,
        monkeypatch,
        synth_flags=["-noshare"],
        arithmetic={"adder_architecture": "sklansky"},
        **HIERARCHICAL,
    )
    extra = ["-extra-map", "+/choices/sklansky.v", "-noshare"]
    assert _segment(calls, ["proc"], 6) == [
        ["proc"],
        ["synth", "-run", ":fine", *extra],
        ["convert_liberty_areas"],
        ["keep_hierarchy", "-min_cost", "5"],
        ["synth", "-flatten", "-run", "coarse:fine", *extra],
        ["synth", "-top", "mac", "-run", "fine:", "-noabc", *extra],
    ]


def test_hierarchical_synthesis_defines_the_area_conversion_with_its_source(tmp_path, monkeypatch):
    """`convert_liberty_areas` is a procedure of the script. It names where it comes from."""
    run_dir, _ = _render(tmp_path, monkeypatch, **HIERARCHICAL)
    script = (run_dir / "yosys_synth.tcl").read_text()
    assert re.search(r"^proc convert_liberty_areas \{ ?\} \{$", script, re.M)
    assert "6b305a712966be72d8908695c04398acdbe273ca" in script and "BSD-3-Clause" in script
    # the other mode and the default define no procedure
    other, _ = _render(tmp_path / "o", monkeypatch, **ABC_NEW)
    assert "convert_liberty_areas" not in (other / "yosys_synth.tcl").read_text()


def test_hierarchical_synthesis_with_rtl_output_writes_the_processed_design(tmp_path, monkeypatch):
    """`write_json` refuses a module with processes, so `proc` stays before the RTL outputs."""
    _, calls = _render(tmp_path, monkeypatch, rtl_json="rtl.json", **HIERARCHICAL)
    names = [call[0] for call in calls]
    assert names.index("proc") < names.index("write_json") < names.index("synth")


# ------------------------------------------------------------------- abc_new and wrapping


def test_abc_new_maps_with_its_own_script_and_the_script_file_written_for_it(tmp_path, monkeypatch):
    """`abc_new` is not given `-script`: it would drop its default script for a path that holds
    that text. The file is a relative name in the run directory."""
    settings = _tiny(tmp_path, script_format="tcl", **ABC_NEW)
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, settings)
    calls = _calls(run_dir, tmp_path)
    lib = _liberty(settings)
    unusable = [word for cell in TINY_DONT_USE for word in ("-dont_use", cell)]
    assert _segment(
        calls, ["scratchpad", "-set", "abc9.script", "abc_speed_gia_only.script"], 3
    ) == [
        ["scratchpad", "-set", "abc9.script", "abc_speed_gia_only.script"],
        ["abc_new", "-liberty", lib, *unusable, "-constr", "abc.constr"],
        ["delete", "t:$specify*"],
    ]
    assert "abc" not in [call[0] for call in calls]
    assert (run_dir / "abc_speed_gia_only.script").is_file()


def test_abc_new_alone_keeps_the_rest_of_the_mapping(tmp_path, monkeypatch):
    """No wrapping: the plain `synth`, the full `check`, and the netlist without attributes."""
    _, calls = _render(tmp_path, monkeypatch, **ABC_NEW)
    assert ["synth", "-top", "mac", "-run", "fine:", "-noabc"] in calls
    assert not _words(calls, "wrap_operators") and not _words(calls, "source")
    assert ["check", "-assert", "-mapped"] in calls
    assert "-noattr" in calls[-1] and calls[-1][0] == "write_verilog"


def test_the_delay_target_and_the_abc_flags_follow_the_constraints_on_abc_new(
    tmp_path, monkeypatch
):
    _, calls = _render(
        tmp_path, monkeypatch, abc_flags=["-fast"], abc={"mapper": "abc_new", "delay_target": 1500}
    )
    (abc_new,) = _words(calls, "abc_new")
    assert abc_new[abc_new.index("-constr") :] == [
        "-constr",
        "abc.constr",
        "-D",
        "1500.000",
        "-fast",
    ]


def test_wrapping_sources_the_script_once_and_wraps_in_place_of_the_second_synth(
    tmp_path, monkeypatch
):
    _, calls = _render(tmp_path, monkeypatch, **WRAPPED)
    assert _words(calls, "source") == [["source", WRAP_SCRIPT]]
    assert _words(calls, "wrap_operators") == [["wrap_operators", BOOTH_MAP, "", ""]]
    assert ["synth", "-top", "mac", "-run", "fine:", "-noabc"] not in calls
    assert _segment(calls, ["synth", "-flatten", "-run", ":fine"], 4) == [
        ["synth", "-flatten", "-run", ":fine"],
        ["source", WRAP_SCRIPT],
        ["wrap_operators", BOOTH_MAP, "", ""],
        ["chformal", "-remove"],
    ]


def test_wrapping_maps_the_adders_only_when_no_operator_was_extracted(tmp_path, monkeypatch):
    """The wrapped operators already hold the adders of their architecture: the full adders are
    extracted only if no cell implements an operator."""
    for extracted in (False, True):
        sub = tmp_path / str(extracted)
        settings = _tiny(sub, script_format="tcl", **WRAPPED)
        run_dir = _scripted_launch(sub, monkeypatch, Yosys, settings)
        calls = _calls(run_dir, sub, extracted)
        adders = str(Path(settings["pdk_root"]) / "tiny" / "cells_adders.v")
        asked = calls.index(["design_has_extracted_operators"])
        assert calls[asked - 1] == ["opt", "-purge"]
        assert (["extract_fa"] in calls) is not extracted
        assert (["techmap", "-map", adders] in calls) is not extracted
        assert calls[asked + 1 :].count(["techmap"]) == 1


def test_a_script_without_wrapping_maps_the_adders_without_asking(tmp_path, monkeypatch):
    _, calls = _render(tmp_path, monkeypatch)
    assert ["design_has_extracted_operators"] not in calls and ["extract_fa"] in calls


def test_wrapping_checks_the_design_without_the_mapped_option_and_keeps_the_attributes(
    tmp_path, monkeypatch
):
    """The wrapped modules leave `$buf` cells, which `check -mapped` refuses. The place and route
    tool needs the attributes of the netlist, so `-noattr` is not given."""
    run_dir, calls = _render(tmp_path, monkeypatch, **WRAPPED)
    checks = [call for call in calls if call[0] == "check"]
    assert ["check", "-assert"] in checks and ["check", "-assert", "-mapped"] not in checks
    assert "-noattr" not in calls[-1] and calls[-1][0] == "write_verilog"
    effective = json.loads((run_dir / "settings.json").read_text())["effective_flow_settings"]
    assert effective["netlist_attrs"] is True


def test_wrapping_with_check_assert_off_runs_a_plain_check(tmp_path, monkeypatch):
    _, calls = _render(tmp_path, monkeypatch, check_assert=False, **WRAPPED)
    assert ["check"] in calls and ["check", "-mapped"] not in calls


def test_both_modes_together(tmp_path, monkeypatch):
    _, calls = _render(tmp_path, monkeypatch, **HIERARCHICAL, **WRAPPED)
    assert _segment(calls, ["proc"], 6)[:5] == [
        ["proc"],
        ["synth", "-run", ":fine"],
        ["convert_liberty_areas"],
        ["keep_hierarchy", "-min_cost", "5"],
        ["synth", "-flatten", "-run", "coarse:fine"],
    ]
    assert _segment(calls, ["synth", "-flatten", "-run", "coarse:fine"], 3) == [
        ["synth", "-flatten", "-run", "coarse:fine"],
        ["source", WRAP_SCRIPT],
        ["wrap_operators", BOOTH_MAP, "", ""],
    ]
    assert len(_words(calls, "read_liberty")) == 2


# ----------------------------------------------------------------------- the script file


def _abc_script_lines() -> list[str]:
    return ["&st", "&dch -r", "&nf"] + ["&st", "&syn2", "&if -g -K 6", "&synch2 -r", "&nf"] * 5


def test_the_script_of_abc_new_is_the_gia_only_script_of_openroad_flow_scripts():
    assert ABC_NEW_SCRIPT == "abc_speed_gia_only.script"
    assert ABC_SPEED_GIA_ONLY.splitlines() == _abc_script_lines()
    assert ABC_SPEED_GIA_ONLY.endswith("\n")
    assert "-script" not in ABC_NEW_SCRIPT


def test_the_script_file_is_written_in_the_run_directory_only_for_abc_new(tmp_path, monkeypatch):
    run_dir, _ = _render(tmp_path, monkeypatch, **ABC_NEW)
    assert (run_dir / ABC_NEW_SCRIPT).read_text() == ABC_SPEED_GIA_ONLY
    other, _ = _render(tmp_path / "plain", monkeypatch)
    assert not (other / ABC_NEW_SCRIPT).exists()


def test_a_link_at_the_name_of_the_script_file_is_replaced_and_its_target_is_not_written(
    tmp_path, monkeypatch
):
    """The file goes through the write guard of the run directory, as the abc constraints do: a
    tool may have left a link at its name, which the flow replaces by a file."""
    settings = _tiny(tmp_path, script_format="tcl", **ABC_NEW)
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, settings)
    script = run_dir / ABC_NEW_SCRIPT
    outside = tmp_path / "outside.txt"
    outside.write_text("the file of the user\n")
    script.unlink()
    script.symlink_to(outside)
    assert _scripted_launch(tmp_path, monkeypatch, Yosys, settings) == run_dir
    assert not script.is_symlink() and script.read_text() == ABC_SPEED_GIA_ONLY
    assert outside.read_text() == "the file of the user\n"


# --------------------------------------------------------------------------- the refusals


def _refused(**given: Any) -> dict[str, str]:
    """Plan `given` and return what `check_settings_supported` refuses: the messages of each key,
    joined."""
    settings = Yosys.Settings.from_input(given)
    with pytest.raises(FlowSettingsError) as refused:
        Yosys.check_settings_supported(settings)
    problems: dict[str, list[str]] = {}
    for key, message, _, _ in refused.value.errors:
        problems.setdefault(key, []).append(message)
    return {key: " | ".join(messages) for key, messages in problems.items()}


@pytest.mark.parametrize(
    "key,given",
    [
        ("hierarchy.keep_min_cost", HIERARCHICAL),
        ("arithmetic.wrap_operators", {"arithmetic": {"wrap_operators": True}}),
        ("abc.mapper", ABC_NEW),
    ],
)
def test_the_tcl_only_values_are_refused_with_the_ys_script_and_accepted_with_tcl(key, given):
    problems = _refused(platform="nangate45", script_format="ys", **given)
    assert problems[key].startswith(f"`{key}` ") and "needs `script_format: tcl`" in problems[key]
    accepted = {**given, **ABC_NEW} if key == "arithmetic.wrap_operators" else given
    Yosys.check_settings_supported(
        Yosys.Settings(platform="nangate45", script_format="tcl", **accepted)
    )


def test_the_hierarchical_mode_flattens_all_the_other_modules_and_refuses_flatten_false():
    problems = _refused(platform="nangate45", script_format="tcl", flatten=False, **HIERARCHICAL)
    message = problems["hierarchy.keep_min_cost"]
    assert "`hierarchy.keep_min_cost`" in message and "`flatten: false`" in message
    Yosys.check_settings_supported(
        Yosys.Settings(platform="nangate45", script_format="tcl", flatten=True, **HIERARCHICAL)
    )


def test_wrapping_refuses_the_abc_mapper():
    problems = _refused(
        platform="nangate45", script_format="tcl", arithmetic={"wrap_operators": True}
    )
    assert (
        "operator wrapping maps with `abc_new`: set `abc.mapper: abc_new`"
        in problems["arithmetic.wrap_operators"]
    )


def test_wrapping_refuses_a_netlist_without_attributes():
    problems = _refused(platform="nangate45", script_format="tcl", netlist_attrs=False, **WRAPPED)
    message = problems["netlist_attrs"]
    for word in ("`netlist_attrs: false`", "arithmetic.wrap_operators", "architecture"):
        assert word in message, message
    Yosys.check_settings_supported(
        Yosys.Settings(platform="nangate45", script_format="tcl", netlist_attrs=True, **WRAPPED)
    )
    Yosys.check_settings_supported(
        Yosys.Settings(platform="nangate45", script_format="tcl", netlist_attrs=False, **ABC_NEW)
    )


def test_abc_new_refuses_a_strategy_and_an_abc_script_naming_both():
    problems = _refused(
        platform="nangate45",
        script_format="tcl",
        abc_script="+strash",
        abc={"mapper": "abc_new", "strategy": "speed"},
    )
    for key, setting in (("abc.strategy", "abc.strategy"), ("abc_script", "abc_script")):
        message = problems[key]
        assert f"`{setting}`" in message and "`abc.mapper: abc_new`" in message, message
    # the default strategy is no choice, and the strategy `yosys` is one
    Yosys.check_settings_supported(
        Yosys.Settings(platform="nangate45", script_format="tcl", **ABC_NEW)
    )
    problems = _refused(
        platform="nangate45", script_format="tcl", abc={"mapper": "abc_new", "strategy": "yosys"}
    )
    assert list(problems) == ["abc.strategy"]


def test_the_tcl_modes_arrive_at_planning_before_any_run_directory(tmp_path):
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with pytest.raises(FlowSettingsError, match="abc.mapper"):
        runner.plan(
            Yosys,
            _design(tmp_path / "design"),
            flow_settings=["platform=nangate45", "script_format=tcl", "abc.mapper=abc_new"]
            + ["abc.strategy=speed"],
        )
    assert not (tmp_path / "xeda_run").exists()


# ------------------------------------------------------------------ the opt-in layer itself


def test_a_test_that_needs_a_yosys_with_tcl_skips_naming_the_variable(monkeypatch):
    monkeypatch.delenv("XEDA_TESTS_YOSYS_TCL", raising=False)
    with pytest.raises(pytest.skip.Exception, match="XEDA_TESTS_YOSYS_TCL"):
        require_yosys_tcl()


def test_a_yosys_without_tcl_fails_a_test_that_asked_for_one(monkeypatch, tmp_path):
    """Asked for, the layer is never a skip, whatever `XEDA_TESTS_REQUIRE_TOOLS` says."""
    fake = tmp_path / "bin"
    fake.mkdir()
    (fake / "yosys").write_text(
        "#!/bin/sh\necho 'Error parsing options: Option c does not exist'\nexit 1\n"
    )
    (fake / "yosys").chmod(0o755)
    monkeypatch.setenv("XEDA_TESTS_YOSYS_TCL", "1")
    monkeypatch.setenv("XEDA_TESTS_REQUIRE_TOOLS", "")
    monkeypatch.setenv("PATH", str(fake))
    with pytest.raises(pytest.fail.Exception, match="XEDA_TESTS_YOSYS_TCL"):
        require_yosys_tcl()
    monkeypatch.setenv("PATH", str(tmp_path / "nowhere"))
    with pytest.raises(pytest.fail.Exception, match="XEDA_TESTS_YOSYS_TCL"):
        require_yosys_tcl()


# ----------------------------------------------------------------------- real yosys, with Tcl

ADDER = (
    "module adder(input clk, input [15:0] a, b, output reg [15:0] q);\n"
    "  always @(posedge clk) q <= a + b;\n"
    "endmodule\n"
)

#: `big` costs far more than 30 NAND2 gate equivalents and `small` costs one.
HIERARCHY_DESIGN = (
    "module big(input [15:0] a, b, output [15:0] y);\n"
    "  assign y = (a ^ b) & (a + b);\n"
    "endmodule\n"
    "module small(input a, output y);\n"
    "  assign y = ~a;\n"
    "endmodule\n"
    "module top(input clk, input [15:0] a, b, input c, output reg [15:0] q, output reg r);\n"
    "  wire [15:0] y;\n"
    "  wire s;\n"
    "  big u_big(.a(a), .b(b), .y(y));\n"
    "  small u_small(.a(c), .y(s));\n"
    "  always @(posedge clk) begin q <= y; r <= s; end\n"
    "endmodule\n"
)


def _launch(tmp_path: Path, source: str, top: str, **given: Any):
    root = tmp_path / "design"
    root.mkdir()
    (root / "top.v").write_text(source)
    design = Design(
        name=top, design_root=root, rtl={"sources": ["top.v"], "top": top, "clock": {"port": "clk"}}
    )
    flow = DefaultRunner(tmp_path / "xeda_run", display_results=False).launch_flow(
        Yosys, design, _tiny(tmp_path, script_format="tcl", **given)
    )
    assert flow.succeeded
    return flow, (flow.run_path / "netlist.v").read_text()


def _modules(netlist: str) -> dict[str, str]:
    """The text of each module of a Verilog netlist, by name, with the attributes before it."""
    found = {}
    for match in re.finditer(
        r"((?:\(\*[^\n]*\*\)\s*)*)module\s+(\S+?)\s*\(.*?endmodule", netlist, re.S
    ):
        found[match.group(2)] = match.group(0)
    return found


def test_real_yosys_wraps_a_wide_adder_in_modules_with_the_attributes_for_the_swap(tmp_path):
    require_yosys_tcl()
    _, netlist = _launch(
        tmp_path, ADDER, "adder", arithmetic={"wrap_operators": True}, abc={"mapper": "abc_new"}
    )
    modules = _modules(netlist)
    wrapped = [name for name in modules if name.startswith("ALU_")]
    assert wrapped, netlist
    top = modules["adder"]
    assert any(re.search(rf"^\s*{re.escape(name)}\s", top, re.M) for name in wrapped), top
    for name in wrapped:
        text = netlist[: netlist.index(f"module {name}")]
        header = text[text.rfind("endmodule") + 1 :]
        for attribute in (
            "arithmetic_operator",
            "implements_operator",
            "architecture",
            "source_cell",
        ):
            assert attribute in header, (name, attribute, header)
    assert "1'bx" not in netlist


def test_real_yosys_keeps_a_module_above_the_cost_and_flattens_one_below(tmp_path):
    require_yosys_tcl()
    _, netlist = _launch(tmp_path, HIERARCHY_DESIGN, "top", hierarchy={"keep_min_cost": 30})
    modules = _modules(netlist)
    assert "big" in modules and "top" in modules, list(modules)
    assert "small" not in modules, list(modules)
    assert re.search(r"^\s*big\s", modules["top"], re.M)


def test_real_yosys_maps_with_abc_new_alone(tmp_path):
    require_yosys_tcl()
    _, netlist = _launch(tmp_path, ADDER, "adder", abc={"mapper": "abc_new"})
    assert re.search(r"^\s*(INV|NAND2|NOR2|BUF)_X\d\b", netlist, re.M)
    assert not re.search(r"^\s*\\\$_", netlist, re.M), "a generic cell is left in the netlist"


def test_real_yosys_runs_both_modes_together(tmp_path):
    require_yosys_tcl()
    _, netlist = _launch(
        tmp_path,
        HIERARCHY_DESIGN,
        "top",
        hierarchy={"keep_min_cost": 30},
        arithmetic={"wrap_operators": True},
        abc={"mapper": "abc_new"},
    )
    modules = _modules(netlist)
    assert "big" in modules and "small" not in modules
    assert any(name.startswith("ALU_") for name in modules)


def test_the_bundled_assets_are_where_the_script_sources_them():
    assert Path(WRAP_SCRIPT).is_file() and Path(BOOTH_MAP).is_file()
