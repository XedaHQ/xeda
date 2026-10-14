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
#: The files of the package that wrapping reads. The run directory holds a copy of each, and the
#: script names the copy by its name in the run directory, since a container mounts only that.
WRAP_SCRIPT = "synth_wrap_operators.tcl"
BOOTH_MAP = "synth_wrap_operators-booth.v"

#: The harness of the other Tcl tests, with the commands that the script defines or sources
#: replaced by stubs: `source` records itself and defines nothing, `convert_liberty_areas` and
#: `check_memory_sizes` record themselves instead of querying a design, and
#: `design_has_extracted_operators` records itself and answers `EXTRACTED`. `wrap_operators` is not defined, so it is recorded as any yosys command.
STUBS = """\
rename proc _real_proc
rename source _real_source
set ::extracted EXTRACTED
_real_proc proc {name arglist body} {
  if {$name eq "convert_liberty_areas"} {
    set body {lappend ::calls [list convert_liberty_areas]}
  } elseif {$name eq "check_memory_sizes"} {
    set arglist args
    set body {lappend ::calls [list check_memory_sizes {*}$args]}
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
        synth_flags=["-relativeshare"],
        arithmetic={"adder_architecture": "sklansky"},
        **HIERARCHICAL,
    )
    extra = ["-extra-map", "+/choices/sklansky.v", "-relativeshare"]
    assert _segment(calls, ["proc"], 6) == [
        ["proc"],
        ["synth", "-run", ":fine", *extra],
        ["convert_liberty_areas"],
        ["keep_hierarchy", "-min_cost", "5"],
        ["synth", "-flatten", "-run", "coarse:fine", *extra],
        ["synth", "-top", "mac", "-run", "fine:", "-noabc", *extra],
    ]


def test_hierarchical_synthesis_gives_the_coarse_options_to_both_coarse_runs_and_hieropt_to_two(
    tmp_path, monkeypatch
):
    """The first run only decides what to keep. The run that flattens and the fine run optimize
    across the boundaries of what stays."""
    _, calls = _render(
        tmp_path,
        monkeypatch,
        coarse={"fsm": False},
        hierarchy={"keep_min_cost": 5, "cross_boundary_opt": True},
    )
    assert _segment(calls, ["proc"], 6) == [
        ["proc"],
        ["synth", "-run", ":fine", "-nofsm"],
        ["convert_liberty_areas"],
        ["keep_hierarchy", "-min_cost", "5"],
        ["synth", "-flatten", "-run", "coarse:fine", "-nofsm", "-hieropt"],
        ["synth", "-top", "mac", "-run", "fine:", "-noabc", "-hieropt"],
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


@pytest.mark.parametrize(
    "adders,multipliers,words",
    [
        (
            ["sklansky", "brent_kung", "han_carlson", "kogge_stone"],
            ["base", "booth"],
            ("SKLANSKY BRENT_KUNG HAN_CARLSON KOGGE_STONE", "BASE BOOTH"),
        ),
        (["kogge_stone"], ["booth"], ("KOGGE_STONE", "BOOTH")),
        ([], [], ("", "")),
    ],
)
def test_wrapping_hands_the_architectures_to_the_wrapping_script_in_its_spelling(
    tmp_path, monkeypatch, adders, multipliers, words
):
    _, calls = _render(
        tmp_path,
        monkeypatch,
        arithmetic={
            "wrap_operators": True,
            "wrapped_adders": adders,
            "wrapped_multipliers": multipliers,
        },
        abc={"mapper": "abc_new"},
    )
    assert _words(calls, "wrap_operators") == [["wrap_operators", BOOTH_MAP, *words]]


def test_wrapping_with_the_options_of_extract_fa_keeps_them_for_the_adder_map(
    tmp_path, monkeypatch
):
    _, calls = _render(
        tmp_path,
        monkeypatch,
        arithmetic={"wrap_operators": True, "extract_fa": {"max_depth": 9}},
        abc={"mapper": "abc_new"},
    )
    assert _words(calls, "extract_fa") == [["extract_fa", "-d", "9", "-fa", "-ha"]]


# ------------------------------------------------------------------------- the memory limit


def test_a_memory_limit_runs_the_check_after_the_coarse_part_and_before_the_fine_part(
    tmp_path, monkeypatch
):
    run_dir, calls = _render(tmp_path, monkeypatch, memory={"max_bits": 1024, "mock_large": True})
    assert _segment(calls, ["synth", "-flatten", "-run", ":fine"], 3) == [
        ["synth", "-flatten", "-run", ":fine"],
        ["check_memory_sizes", "1024", "1", "1"],
        ["synth", "-top", "mac", "-run", "fine:", "-noabc"],
    ]
    script = (run_dir / "yosys_synth.tcl").read_text()
    assert re.search(r"^proc check_memory_sizes \{ max_bits mock keep \} \{$", script, re.M)
    assert "6b305a712966be72d8908695c04398acdbe273ca" in script and "BSD-3-Clause" in script


@pytest.mark.parametrize(
    "memory,arguments",
    [
        ({"max_bits": 64}, ["64", "0", "1"]),
        ({"max_bits": 64, "mock_large": True, "keep_mocked": False}, ["64", "1", "0"]),
    ],
)
def test_the_memory_check_gets_the_limit_and_whether_to_mock_and_to_keep(
    tmp_path, monkeypatch, memory, arguments
):
    _, calls = _render(tmp_path, monkeypatch, memory=memory)
    assert _words(calls, "check_memory_sizes") == [["check_memory_sizes", *arguments]]


def test_the_memory_libraries_come_before_the_memory_check(tmp_path, monkeypatch):
    design = tmp_path / "design"
    design.mkdir()
    (design / "mem.txt").write_text("\n")
    (design / "mem_map.v").write_text("\n")
    _, calls = _render(
        tmp_path,
        monkeypatch,
        memory={"libmap": ["mem.txt"], "techmap": "mem_map.v", "max_bits": 64},
    )
    start = calls.index(["synth", "-flatten", "-run", ":fine"])
    assert [call[0] for call in calls[start : start + 4]] == [
        "synth",
        "memory_libmap",
        "techmap",
        "check_memory_sizes",
    ]


def test_without_a_memory_limit_the_script_has_no_memory_check(tmp_path, monkeypatch):
    run_dir, calls = _render(tmp_path, monkeypatch)
    assert _words(calls, "check_memory_sizes") == []
    assert "check_memory_sizes" not in (run_dir / "yosys_synth.tcl").read_text()


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


def test_the_memory_size_checks_are_tcl_only_values(tmp_path):
    problems = _refused(platform="nangate45", script_format="ys", memory={"max_bits": 64})
    assert "needs `script_format: tcl`" in problems["memory.max_bits"]


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


OPERATOR_ATTRIBUTES = ("arithmetic_operator", "implements_operator", "architecture", "source_cell")


@pytest.mark.parametrize("attribute", OPERATOR_ATTRIBUTES)
def test_wrapping_refuses_to_strip_an_attribute_that_the_place_and_route_tool_reads(attribute):
    """`netlist_unset_attributes` removes attributes before any netlist is written. The wrapped
    operators are found and swapped by these four."""
    for spelled in (attribute, "\\" + attribute):
        problems = _refused(
            platform="nangate45",
            script_format="tcl",
            netlist_unset_attributes=["keep", spelled],
            **WRAPPED,
        )
        message = problems["netlist_unset_attributes"]
        assert f"`{attribute}`" in message and "arithmetic.wrap_operators" in message, message
    # without wrapping the attribute is the user's to strip, and other attributes are fine
    Yosys.check_settings_supported(
        Yosys.Settings(
            platform="nangate45",
            script_format="tcl",
            netlist_unset_attributes=[attribute],
            **ABC_NEW,
        )
    )
    Yosys.check_settings_supported(
        Yosys.Settings(
            platform="nangate45",
            script_format="tcl",
            netlist_unset_attributes=["keep", "abc9_box"],
            **WRAPPED,
        )
    )


def test_the_script_strips_only_the_attributes_that_the_settings_name(tmp_path, monkeypatch):
    """With wrapping the four attributes are never in a `setattr -unset`, so no other setting
    removes them before the netlist is written."""
    _, calls = _render(tmp_path, monkeypatch, netlist_unset_attributes=["keep"], **WRAPPED)
    unset = [call[-2] for call in calls if call[:2] == ["setattr", "-unset"]]
    assert sorted(unset) == ["keep", "src"]
    assert not set(OPERATOR_ATTRIBUTES) & {word for call in calls for word in call[:-1]}


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


RAM = (
    "module top(input clk, input we, input [7:0] addr, input [7:0] din, output reg [7:0] dout);\n"
    "  reg [7:0] mem [0:255];\n"
    "  always @(posedge clk) begin\n"
    "    if (we) mem[addr] <= din;\n"
    "    dout <= mem[addr];\n"
    "  end\n"
    "endmodule\n"
)


def _launch_ram(tmp_path: Path, **given: Any):
    root = tmp_path / "design"
    root.mkdir(parents=True)
    (root / "top.v").write_text(RAM)
    design = Design(
        name="top",
        design_root=root,
        rtl={"sources": ["top.v"], "top": "top", "clock": {"port": "clk"}},
    )
    return DefaultRunner(tmp_path / "xeda_run", display_results=False).launch_flow(
        Yosys, design, _tiny(tmp_path, script_format="tcl", **given)
    )


def test_real_yosys_accepts_a_memory_within_the_limit(tmp_path):
    require_yosys_tcl()
    flow = _launch_ram(tmp_path, memory={"max_bits": 2048})
    assert flow.succeeded
    assert (
        "Memory top/mem has dimensions 256 x 8 = 2048" in (flow.run_path / "yosys.log").read_text()
    )


def test_real_yosys_stops_at_a_memory_above_the_limit_and_names_the_setting(tmp_path):
    require_yosys_tcl()
    flow = _launch_ram(tmp_path, memory={"max_bits": 2047})
    assert not flow.succeeded
    log = (flow.run_path / "yosys.log").read_text()
    assert "memory above memory.max_bits (2047 bits): top/mem (2048 bits)" in log


def test_real_yosys_shrinks_a_memory_above_the_limit_to_one_row(tmp_path):
    require_yosys_tcl()
    flow = _launch_ram(tmp_path, memory={"max_bits": 1024, "mock_large": True})
    assert flow.succeeded
    log = (flow.run_path / "yosys.log").read_text()
    assert "Shrunk memory top/mem from 256 rows to 1" in log
    full = _launch_ram(tmp_path / "full")
    assert full.succeeded
    assert flow.results["cells"] < full.results["cells"] // 4


def test_real_yosys_wraps_with_the_architectures_of_the_settings(tmp_path):
    require_yosys_tcl()
    _, netlist = _launch(
        tmp_path,
        ADDER,
        "adder",
        arithmetic={
            "wrap_operators": True,
            "wrapped_adders": ["sklansky", "kogge_stone"],
        },
        abc={"mapper": "abc_new"},
    )
    assert re.search(r'architecture = "SKLANSKY"|architecture = "sklansky"', netlist, re.I)
    assert not re.search(r'architecture = "BRENT_KUNG"', netlist, re.I), netlist


MULTIPLIER = (
    "module mult(input clk, input [11:0] a, b, output reg [23:0] q);\n"
    "  always @(posedge clk) q <= a * b;\n"
    "endmodule\n"
)


@pytest.mark.parametrize("multipliers", [["base"], ["booth", "base"]])
def test_real_yosys_takes_the_wrapped_multipliers_of_the_settings(tmp_path, multipliers):
    """The wrapping script of OpenROAD-flow-scripts selects `$macc` cells by `A_WIDTH`, which a
    `$macc` does not have. So only the adders of the multiplier show as wrapped. The run must
    take the order and finish."""
    require_yosys_tcl()
    _, netlist = _launch(
        tmp_path,
        MULTIPLIER,
        "mult",
        arithmetic={"wrap_operators": True, "wrapped_multipliers": multipliers},
        abc={"mapper": "abc_new"},
    )
    assert "ALU_" in netlist


def test_the_bundled_assets_are_where_the_script_sources_them():
    assert DATA.joinpath(WRAP_SCRIPT).is_file() and DATA.joinpath(BOOTH_MAP).is_file()


def test_the_script_names_the_wrapping_files_relative_to_the_run_directory(tmp_path, monkeypatch):
    run_dir, calls = _render(tmp_path, monkeypatch, **WRAPPED)
    assert _words(calls, "source") == [["source", WRAP_SCRIPT]]
    assert str(tmp_path) not in " ".join(_words(calls, "wrap_operators")[0])
    assert str(DATA) not in (run_dir / "yosys_synth.tcl").read_text()


def test_the_wrapping_files_are_copied_into_the_run_directory_only_when_wrapping(
    tmp_path, monkeypatch
):
    run_dir, _ = _render(tmp_path, monkeypatch, **WRAPPED)
    for name in (WRAP_SCRIPT, BOOTH_MAP):
        assert (run_dir / name).read_bytes() == DATA.joinpath(name).read_bytes()
    other, _ = _render(tmp_path / "plain", monkeypatch)
    assert not (other / WRAP_SCRIPT).exists() and not (other / BOOTH_MAP).exists()


def test_a_link_at_the_name_of_a_wrapping_file_is_replaced_and_its_target_is_not_written(
    tmp_path, monkeypatch
):
    settings = _tiny(tmp_path, script_format="tcl", **WRAPPED)
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, settings)
    outside = tmp_path / "outside.txt"
    outside.write_text("the file of the user\n")
    for name in (WRAP_SCRIPT, BOOTH_MAP):
        (run_dir / name).unlink()
        (run_dir / name).symlink_to(outside)
    assert _scripted_launch(tmp_path, monkeypatch, Yosys, settings) == run_dir
    for name in (WRAP_SCRIPT, BOOTH_MAP):
        assert not (run_dir / name).is_symlink()
        assert (run_dir / name).read_bytes() == DATA.joinpath(name).read_bytes()
    assert outside.read_text() == "the file of the user\n"
