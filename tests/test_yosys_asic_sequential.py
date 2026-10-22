"""The sequential options of the `yosys` ASIC synthesis: flip-flops, latches, clock gates, retiming
and tri-states.

The script maps the sequential cells in the order of OpenROAD-flow-scripts: the latch map, the
user maps, clock gating, then the flip-flops. Retiming of chosen modules and the tri-state pass
run between the two parts of `synth`. Every rendering test runs for both script formats. The Tcl
script runs under `tclsh` with every yosys command recording itself. The tests at the end run a
real yosys on the `tiny` platform, with a copy of its library that has a clock-gating cell.
"""

import contextlib
import json
import re
from pathlib import Path
from typing import Any, get_args

import pytest

from xeda import Design
from xeda.dataclass import tier_of
from xeda.flow import FlowException
from xeda.flow_runner import DefaultRunner
from xeda.flows import Yosys
from xeda.flows.yosys import asic_groups
from xeda.flows.yosys.yosys import LEGALIZE_ANY_FLIP_FLOP

from .test_yosys_asic import SCRIPT_FORMATS, _design, _words
from .test_yosys_asic_groups import no_liberty_message, _pdk, _refused, _run
from .tiny_platform import tiny_settings, tiny_store
from .tool_utils import require_yosys, require_yosys_tcl
from .yosys_format import forced_script_format

ENABLES = (
    "module en(input clk, input rst, input e1, input e2, input [3:0] d, output reg [3:0] q1,\n"
    "          output reg [3:0] q2, output reg [3:0] q3);\n"
    "  always @(posedge clk) begin\n"
    "    if (e1) q1 <= d;\n"
    "    if (e2) q2 <= d + 1;\n"
    "    if (rst) q3 <= 0; else q3 <= q3 + d;\n"
    "  end\n"
    "endmodule\n"
)

#: A clock-gating cell for the copy of the `tiny` library: a latch and an AND gate.
CLOCK_GATING_CELL = """\
  cell (TINY_ICG) {
    area : 5.000;
    clock_gating_integrated_cell : "latch_posedge";
    latch (IQ, IQN) {
      enable : "!CK";
      data_in : "E";
    }
    pin (CK) {
      direction : input;
      capacitance : 2.0;
      clock_gate_clock_pin : true;
    }
    pin (E) {
      direction : input;
      capacitance : 2.0;
      clock_gate_enable_pin : true;
    }
    pin (GCK) {
      direction : output;
      function : "CK & IQ";
      clock_gate_out_pin : true;
      max_capacitance : 100;
    }
  }
"""

#: A map of the positive-edge flip-flop to the one flip-flop cell of the `tiny` library.
DFF_MAP = """\
// A comment that names no module: module not_a_cell (
module $_DFF_P_ (input D, input C, output Q);
  DFF_X1 _TECHMAP_REPLACE_ (.D(D), .CK(C), .Q(Q));
endmodule
"""

ICG = ["TINY_ICG", "E", "CK", "GCK"]


def _enable_design(root: Path) -> Design:
    root.mkdir(parents=True, exist_ok=True)
    (root / "en.v").write_text(ENABLES)
    return Design(
        name="en",
        design_root=root,
        rtl={"sources": ["en.v"], "top": "en", "clock": {"port": "clk"}},
    )


def _store_with_clock_gate(tmp_path: Path) -> Path:
    """A store of the `tiny` platform whose library has a clock-gating cell."""
    store = tiny_store(tmp_path)
    library = store / "tiny" / "tiny.lib"
    text = library.read_text()
    end = text.rindex("}")
    library.write_text(text[:end] + CLOCK_GATING_CELL + text[end:])
    return store


# ------------------------------------------------------------------------------ the model


def test_the_sequential_leaves_have_the_defaults_that_keep_the_script_as_it_was():
    sequential = Yosys.Settings().sequential
    assert sequential.latch_map == "platform" and sequential.latch_map_file is None
    assert sequential.clockgate_map == "platform" and sequential.clockgate_map_file is None
    assert sequential.dff_liberty is None and sequential.dff_map is None
    assert sequential.retime_modules == []
    assert sequential.tristate == "off"
    assert sequential.min_enable_fanout is None and sequential.min_sync_reset_fanout is None
    gating = sequential.clock_gating
    assert gating.enabled is False
    assert gating.pos_icg is None and gating.neg_icg is None
    assert gating.min_width is None and gating.tie_lo is None


def test_the_closed_names_of_the_sequential_leaves():
    group = asic_groups.SequentialGroup.model_fields
    for name in ("latch_map", "clockgate_map"):
        assert set(get_args(group[name].annotation)) == {"platform", "none"}, name
    assert set(get_args(group["tristate"].annotation)) == {"off", "logic"}


@pytest.mark.parametrize(
    "given,mentions",
    [
        ({"latch_map": "maybe"}, "latch_map"),
        ({"clockgate_map": "maybe"}, "clockgate_map"),
        ({"tristate": "map"}, "tristate"),
        ({"min_enable_fanout": 0}, "min_enable_fanout"),
        ({"min_sync_reset_fanout": 0}, "min_sync_reset_fanout"),
        ({"retime_modules": ["a*"]}, "retime_modules"),
        ({"retime_modules": ["a b"]}, "retime_modules"),
        ({"clock_gating": {"min_width": 0}}, "min_width"),
        ({"clock_gating": {"pos_icg": ["ICG", "E", "CK"]}}, "pos_icg"),
        ({"clock_gating": {"neg_icg": "ICG"}}, "neg_icg"),
        ({"clock_gating": {"tie_lo": ""}}, "tie_lo"),
        ({"clock_gating": True}, "clock_gating"),
    ],
)
def test_a_sequential_leaf_refuses_a_value_outside_its_type(given, mentions):
    with pytest.raises(Exception, match=mentions):
        Yosys.Settings.from_input({"sequential": given})


def test_the_icg_cells_are_a_cell_and_three_ports():
    settings = Yosys.Settings.from_input(
        {"sequential": {"clock_gating": {"enabled": True, "pos_icg": ICG}}}
    )
    assert tuple(settings.sequential.clock_gating.pos_icg) == tuple(ICG)


def test_the_retime_modules_take_comma_separated_text_and_refuse_brackets():
    settings = Yosys.Settings.from_input({"sequential": {"retime_modules": "a,b"}})
    assert settings.sequential.retime_modules == ["a", "b"]
    with pytest.raises(Exception, match="retime_modules"):
        Yosys.Settings.from_input({"sequential": {"retime_modules": "[a]"}})


def test_the_sequential_leaves_have_a_tier_and_a_description():
    group = asic_groups.SequentialGroup
    for leaf, info in group.model_fields.items():
        assert info.description, leaf
        if leaf != "clock_gating":
            assert tier_of(group, leaf) is not None, leaf
    gating = asic_groups.ClockGatingGroup
    for leaf, info in gating.model_fields.items():
        assert info.description and tier_of(gating, leaf) is not None, leaf


def test_the_clock_gating_group_is_switchable():
    assert list(asic_groups.ClockGatingGroup.model_fields)[0] == "enabled"


# ------------------------------------------------------------------------------ what is rendered


@SCRIPT_FORMATS
def test_the_default_settings_add_no_sequential_command(tmp_path, monkeypatch, script_format):
    commands = _run(tmp_path, monkeypatch, script_format)
    for name in ("clockgate", "tribuf", "select", "memory_map", "retime"):
        assert _words(commands, name) == [], name
    assert len(_words(commands, "dfflegalize")) == 1, "the latch map only"
    (dfflibmap,) = _words(commands, "dfflibmap")
    assert dfflibmap[:2] == ["dfflibmap", "-liberty"]
    assert ["techmap", "-map", str(_pdk(tmp_path) / "cells_latch.v")] in commands


@SCRIPT_FORMATS
def test_latch_map_none_runs_no_latch_mapping(tmp_path, monkeypatch, script_format):
    commands = _run(tmp_path, monkeypatch, script_format, sequential={"latch_map": "none"})
    assert _words(commands, "dfflegalize") == []
    assert ["techmap", "-map", str(_pdk(tmp_path) / "cells_latch.v")] not in commands
    assert _words(commands, "dfflibmap"), "the flip-flops stay mapped"


@SCRIPT_FORMATS
def test_clockgate_map_none_reads_no_clock_gate_file(tmp_path, monkeypatch, script_format):
    commands = _run(tmp_path, monkeypatch, script_format, sequential={"clockgate_map": "none"})
    assert [call for call in commands if call[:2] == ["read_verilog", "-defer"]] != []
    assert ["read_verilog", "-defer", str(_pdk(tmp_path) / "cells_clkgate.v")] not in commands


@SCRIPT_FORMATS
def test_the_maps_none_with_a_platform_that_has_them_leave_the_rest_unchanged(
    tmp_path, monkeypatch, script_format
):
    base = _run(tmp_path / "a", monkeypatch, script_format)
    none = _run(
        tmp_path / "b",
        monkeypatch,
        script_format,
        sequential={"latch_map": "none", "clockgate_map": "none"},
    )
    drop = [
        ["dfflegalize", "-cell", "$_DLATCH_P_", "x", "-cell", "$_DLATCH_N_", "x", "t:$_DLATCH_*"],
        ["techmap", "-map", str(_pdk(tmp_path / "a") / "cells_latch.v")],
        ["read_verilog", "-defer", str(_pdk(tmp_path / "a") / "cells_clkgate.v")],
    ]
    assert all(call in base for call in drop)
    same_store = [
        [word.replace(str(tmp_path / "b"), str(tmp_path / "a")) for word in call] for call in none
    ]
    assert same_store == [call for call in base if call not in drop]


@SCRIPT_FORMATS
def test_the_dff_map_replaces_dfflibmap_by_the_legalization_and_the_map(
    tmp_path, monkeypatch, script_format
):
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "dffs.v").write_text(
        DFF_MAP + "module $_DFFE_PP_ (input D, C, E, output Q);\nendmodule\n"
    )
    commands = _run(tmp_path, monkeypatch, script_format, sequential={"dff_map": "dffs.v"})
    assert _words(commands, "dfflibmap") == []
    legalize = [call for call in _words(commands, "dfflegalize") if "-mince" not in call]
    assert legalize[-1] == [
        "dfflegalize",
        *("-cell", "$_DFF_P_", "x", "-cell", "$_DFFE_PP_", "x"),
    ]
    names = [command[0] for command in commands]
    last = len(commands) - 1 - names[::-1].index("dfflegalize")
    assert commands[last + 1] == ["techmap", "-map", str(tmp_path / "design" / "dffs.v")]


@SCRIPT_FORMATS
def test_the_fanout_limits_go_on_the_legalization_of_the_dff_map(
    tmp_path, monkeypatch, script_format
):
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "dffs.v").write_text(DFF_MAP)
    commands = _run(
        tmp_path,
        monkeypatch,
        script_format,
        sequential={"dff_map": "dffs.v", "min_enable_fanout": 4, "min_sync_reset_fanout": 3},
    )
    legalize = _words(commands, "dfflegalize")[-1]
    assert legalize == [
        *("dfflegalize", "-cell", "$_DFF_P_", "x"),
        *("-mince", "4", "-minsrst", "3"),
    ]
    assert len([call for call in _words(commands, "dfflegalize") if "-mince" in call]) == 1


@SCRIPT_FORMATS
@pytest.mark.parametrize(
    "given,flags",
    [
        ({"min_enable_fanout": 5}, ["-mince", "5"]),
        ({"min_sync_reset_fanout": 2}, ["-minsrst", "2"]),
        ({"min_enable_fanout": 5, "min_sync_reset_fanout": 2}, ["-mince", "5", "-minsrst", "2"]),
    ],
    ids=["enable", "sync-reset", "both"],
)
def test_the_fanout_limits_run_a_legalization_that_allows_every_flip_flop_before_dfflibmap(
    tmp_path, monkeypatch, script_format, given, flags
):
    commands = _run(tmp_path, monkeypatch, script_format, sequential=given)
    (legalize,) = [
        call for call in _words(commands, "dfflegalize") if "-mince" in call or "-minsrst" in call
    ]
    cells = [legalize[i + 1] for i, word in enumerate(legalize) if word == "-cell"]
    assert cells == list(LEGALIZE_ANY_FLIP_FLOP)
    assert legalize[-len(flags) :] == flags
    names = [command[0] for command in commands]
    assert commands.index(legalize) < names.index("dfflibmap")


def test_the_legalization_allows_every_cell_type_that_dfflegalize_ingests():
    assert len(set(LEGALIZE_ANY_FLIP_FLOP)) == len(LEGALIZE_ANY_FLIP_FLOP)
    assert "$_DFF_?_" in LEGALIZE_ANY_FLIP_FLOP and "$_DLATCHSR_???_" in LEGALIZE_ANY_FLIP_FLOP


@SCRIPT_FORMATS
def test_clock_gating_picks_the_cell_from_the_liberty_files_when_no_cell_is_named(
    tmp_path, monkeypatch, script_format
):
    commands = _run(
        tmp_path,
        monkeypatch,
        script_format,
        dont_use_cells=["INV_X4", "NAND2_X4"],
        sequential={"clock_gating": {"enabled": True, "min_width": 4}},
    )
    (clockgate,) = _words(commands, "clockgate")
    library = str(_pdk(tmp_path) / "tiny.lib")
    assert clockgate == [
        *("clockgate", "-liberty", library),
        *("-dont_use", "INV_X4", "-dont_use", "NAND2_X4"),
        *("-min_net_size", "4"),
    ]


@SCRIPT_FORMATS
def test_clock_gating_with_named_cells_reads_no_liberty_file_and_needs_no_dont_use(
    tmp_path, monkeypatch, script_format
):
    commands = _run(
        tmp_path,
        monkeypatch,
        script_format,
        dont_use_cells=["INV_X4"],
        sequential={
            "clock_gating": {
                "enabled": True,
                "pos_icg": ICG,
                "neg_icg": ["NEG_ICG", "EN", "CLK", "GCLK"],
                "min_width": 8,
                "tie_lo": "SE",
            }
        },
    )
    (clockgate,) = _words(commands, "clockgate")
    assert clockgate == [
        *("clockgate", "-pos", "TINY_ICG", "E:CK:GCK"),
        *("-neg", "NEG_ICG", "EN:CLK:GCLK"),
        *("-min_net_size", "8", "-tie_lo", "SE"),
    ]


@SCRIPT_FORMATS
def test_clock_gating_runs_after_the_maps_of_latches_and_before_the_flip_flop_mapping(
    tmp_path, monkeypatch, script_format
):
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "extra.v").write_text("module _unused(); endmodule\n")
    commands = _run(
        tmp_path,
        monkeypatch,
        script_format,
        techmap_files=["extra.v"],
        sequential={"clock_gating": {"enabled": True}},
    )
    names = [command[0] for command in commands]
    gating = names.index("clockgate")
    extra = commands.index(["techmap", "-map", str(tmp_path / "design" / "extra.v")])
    assert extra < gating < names.index("dfflibmap")
    assert gating == names.index("dfflibmap") - 1


@SCRIPT_FORMATS
def test_clock_gating_switched_off_runs_no_clockgate(tmp_path, monkeypatch, script_format):
    commands = _run(
        tmp_path, monkeypatch, script_format, sequential={"clock_gating": {"enabled": False}}
    )
    assert _words(commands, "clockgate") == []


RETIME_SCRIPT = "+st;retime,-v,-o;retime,-M,5,-v,-o;retime,-M,5,-v,-o;retime,-M,5,-v,-o;map"


@SCRIPT_FORMATS
def test_retime_modules_run_abc_with_the_retime_script_on_the_selected_modules(
    tmp_path, monkeypatch, script_format
):
    commands = _run(
        tmp_path,
        monkeypatch,
        script_format,
        keep_hierarchy=["mul8"],
        sequential={"retime_modules": ["mac", "mul8"]},
    )
    names = [command[0] for command in commands]
    select = commands.index(["select", "mac", "mul8"])
    assert commands[select - 2 : select] == [
        ["select", "-assert-any", "mac"],
        ["select", "-assert-any", "mul8"],
    ]
    assert commands[select : select + 7] == [
        ["select", "mac", "mul8"],
        ["opt", "-fast", "-full"],
        ["memory_map"],
        ["opt", "-full"],
        ["techmap"],
        ["abc", "-dff", "-script", RETIME_SCRIPT],
        ["select", "-clear"],
    ]
    synths = [i for i, name in enumerate(names) if name == "synth"]
    assert synths[0] < select and select + 6 < synths[1], "between the two parts of synth"


@SCRIPT_FORMATS
def test_no_retime_modules_run_nothing_between_the_two_synth_commands(
    tmp_path, monkeypatch, script_format
):
    commands = _run(tmp_path, monkeypatch, script_format)
    names = [command[0] for command in commands]
    synths = [i for i, name in enumerate(names) if name == "synth"]
    assert names[synths[0] + 1 : synths[1]] == [], names


@SCRIPT_FORMATS
def test_tristate_logic_converts_the_tristate_buffers_after_the_first_synth(
    tmp_path, monkeypatch, script_format
):
    commands = _run(tmp_path, monkeypatch, script_format, sequential={"tristate": "logic"})
    names = [command[0] for command in commands]
    first = names.index("synth")
    assert commands[first + 1 : first + 3] == [["tribuf", "-logic"], ["opt_clean"]]
    assert len(_words(commands, "tribuf")) == 1


@SCRIPT_FORMATS
def test_tristate_off_runs_no_tribuf(tmp_path, monkeypatch, script_format):
    commands = _run(tmp_path, monkeypatch, script_format, sequential={"tristate": "off"})
    assert _words(commands, "tribuf") == []


# ------------------------------------------------------------------------------ the refusals


def _keys(**given: Any) -> dict[str, str]:
    return dict(_refused(platform="nangate45", **given))


@pytest.mark.parametrize("name", ["latch_map", "clockgate_map"])
def test_a_map_file_with_none_is_refused_naming_both(name):
    problems = _keys(sequential={name: "none", f"{name}_file": "m.v"})
    message = problems[f"sequential.{name}_file"]
    assert f"`sequential.{name}_file`" in message and f"`sequential.{name}: none`" in message
    Yosys.check_settings_supported(
        Yosys.Settings(platform="nangate45", sequential={name: "platform", f"{name}_file": "m.v"})
    )


def test_the_dff_map_and_the_dff_liberty_are_one_way_only():
    problems = _keys(sequential={"dff_map": "d.v", "dff_liberty": "d.lib"})
    message = problems["sequential.dff_map"]
    assert "`sequential.dff_map`" in message and "`sequential.dff_liberty`" in message
    Yosys.check_settings_supported(
        Yosys.Settings(platform="nangate45", sequential={"dff_map": "d.v"})
    )
    Yosys.check_settings_supported(
        Yosys.Settings(platform="nangate45", sequential={"dff_liberty": "d.lib"})
    )


def test_a_module_is_retimed_once():
    problems = _keys(sequential={"retime_modules": ["a", "b", "a"]})
    assert "`a`" in problems["sequential.retime_modules"]
    assert "more than once" in problems["sequential.retime_modules"]


def test_the_tie_low_port_needs_a_named_cell():
    problems = _keys(sequential={"clock_gating": {"enabled": True, "tie_lo": "SE"}})
    message = problems["sequential.clock_gating.tie_lo"]
    assert "`sequential.clock_gating.tie_lo`" in message
    assert "`sequential.clock_gating.pos_icg`" in message
    Yosys.check_settings_supported(
        Yosys.Settings(
            platform="nangate45",
            sequential={"clock_gating": {"enabled": True, "tie_lo": "SE", "neg_icg": ICG}},
        )
    )


@pytest.mark.parametrize(
    "leaf,value",
    [("pos_icg", ICG), ("neg_icg", ICG), ("min_width", 4), ("tie_lo", "SE")],
)
def test_a_clock_gating_leaf_is_refused_while_clock_gating_is_off(leaf, value):
    problems = _keys(sequential={"clock_gating": {leaf: value}})
    message = problems[f"sequential.clock_gating.{leaf}"]
    assert "has no effect while `sequential.clock_gating.enabled` is false" in message


@pytest.mark.parametrize(
    "key,given",
    [
        ("sequential.latch_map", {"sequential": {"latch_map": "none"}}),
        ("sequential.latch_map_file", {"sequential": {"latch_map_file": "l.v"}}),
        ("sequential.clockgate_map", {"sequential": {"clockgate_map": "none"}}),
        ("sequential.clockgate_map_file", {"sequential": {"clockgate_map_file": "c.v"}}),
        ("sequential.dff_map", {"sequential": {"dff_map": "d.v"}}),
        (
            "sequential.clock_gating.enabled",
            {"sequential": {"clock_gating": {"enabled": True}}},
        ),
        ("sequential.retime_modules", {"sequential": {"retime_modules": ["a"]}}),
        ("sequential.tristate", {"sequential": {"tristate": "logic"}}),
        ("sequential.min_enable_fanout", {"sequential": {"min_enable_fanout": 3}}),
        ("sequential.min_sync_reset_fanout", {"sequential": {"min_sync_reset_fanout": 3}}),
    ],
)
@pytest.mark.parametrize("mapping", [{"gates": ["AND"]}, {}], ids=str)
def test_a_sequential_leaf_is_refused_when_no_liberty_mapping_runs(key, given, mapping):
    problems = dict(_refused(**given, **mapping))
    assert problems[key] == no_liberty_message(key, mapping)


def test_retime_modules_assert_each_module_before_the_retiming_in_both_formats_only_when_set(
    tmp_path, monkeypatch
):
    for script_format in ("ys", "tcl"):
        commands = _run(tmp_path / script_format, monkeypatch, script_format)
        assert ["select", "-assert-any"] not in [command[:2] for command in commands]


# ------------------------------------------------------------------------------ input files


def test_the_dff_map_names_the_cells_it_maps_and_a_file_without_a_module_is_refused(tmp_path):
    from xeda.flows.yosys.yosys import dff_map_cells

    path = tmp_path / "map.v"
    path.write_text(DFF_MAP + "  module $_DFFE_PP_ (input D);\nendmodule\n")
    assert dff_map_cells(path) == ("$_DFF_P_", "$_DFFE_PP_")
    path.write_text("// no module here\nwire x;\n")
    with pytest.raises(FlowException, match="names no module"):
        dff_map_cells(path)


def test_the_new_map_files_resolve_against_the_design_root(tmp_path):
    from xeda.flows.yosys import yosys as module

    store = tiny_store(tmp_path)
    design = _design(tmp_path / "design")
    (design.design_root / "dffs.v").write_text(DFF_MAP)
    settings = Yosys.Settings.from_input(
        tiny_settings(store, sequential={"dff_map": "dffs.v"}), design_root=design.design_root
    )
    run = tmp_path / "run"
    run.mkdir()
    flow = Yosys(settings, design, run)
    flow.init()
    flow.prepare_inputs()
    values = flow.asic_values()
    assert values.dff_map == design.design_root / "dffs.v"
    assert values.dff_map_cells == ("$_DFF_P_",)
    assert values.legalize_any == module.LEGALIZE_ANY_FLIP_FLOP


# ------------------------------------------------------------------------------ real yosys


def _run_real(tmp_path: Path, script_format: str, **given: Any):
    """Run `yosys` on the `en` design and the `tiny` platform with a clock-gating cell."""
    store = _store_with_clock_gate(tmp_path)
    design = _enable_design(tmp_path / "design")
    settings = tiny_settings(store, clock={"period": "2 ns"}, **given)
    with contextlib.suppress(FlowException), forced_script_format(script_format):
        DefaultRunner(tmp_path / "xeda_run").run_flow(Yosys, design, settings)
    (results,) = tmp_path.glob("xeda_run/en/yosys/results.json")
    assert json.loads(results.read_text())["outcome"] == "passed", results.read_text()
    assert (results.parent / f"yosys_synth.{script_format}").is_file()
    return results.parent


def _real(script_format: str) -> None:
    if script_format == "ys":
        require_yosys()
    else:
        require_yosys_tcl()


FORMATS_REAL = pytest.mark.parametrize("script_format", ["ys", "tcl"])


@FORMATS_REAL
def test_real_yosys_maps_the_flip_flops_with_the_dff_map(tmp_path, script_format):
    _real(script_format)
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "dffs.v").write_text(DFF_MAP)
    run = _run_real(tmp_path, script_format, sequential={"dff_map": "dffs.v"})
    assert "DFF_X1" in (run / "netlist.v").read_text()
    assert "Executing DFFLIBMAP pass" not in (run / "yosys.log").read_text()


@FORMATS_REAL
def test_real_yosys_unmaps_the_enables_and_resets_that_few_flip_flops_share(
    tmp_path, script_format
):
    _real(script_format)
    run = _run_real(
        tmp_path,
        script_format,
        sequential={"min_enable_fanout": 5, "min_sync_reset_fanout": 5},
    )
    script = (run / f"yosys_synth.{script_format}").read_text()
    assert re.search(r'-mince "?5"? -minsrst "?5"?', script)
    log = (run / "yosys.log").read_text()
    assert "Executing DFFLEGALIZE pass" in log and "Executing DFFLIBMAP pass" in log


@FORMATS_REAL
def test_real_yosys_unmaps_the_enables_with_the_dff_map_too(tmp_path, script_format):
    _real(script_format)
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "dffs.v").write_text(DFF_MAP)
    run = _run_real(
        tmp_path,
        script_format,
        sequential={"dff_map": "dffs.v", "min_enable_fanout": 5, "min_sync_reset_fanout": 5},
    )
    assert "DFF_X1" in (run / "netlist.v").read_text()


@FORMATS_REAL
def test_real_yosys_gates_the_clocks_with_the_cell_it_picks_from_the_library(
    tmp_path, script_format
):
    _real(script_format)
    run = _run_real(
        tmp_path, script_format, sequential={"clock_gating": {"enabled": True, "min_width": 3}}
    )
    assert (
        "Selected rising edge ICG \\TINY_ICG from Liberty file" in (run / "yosys.log").read_text()
    )
    assert (run / "netlist.v").read_text().count("TINY_ICG ") == 2


@FORMATS_REAL
def test_real_yosys_gates_the_clocks_with_the_cell_the_setting_names(tmp_path, script_format):
    _real(script_format)
    run = _run_real(
        tmp_path,
        script_format,
        sequential={"clock_gating": {"enabled": True, "pos_icg": ICG}},
    )
    assert (run / "netlist.v").read_text().count("TINY_ICG ") == 2


@FORMATS_REAL
def test_real_yosys_gates_no_clock_for_fewer_flip_flops_than_the_minimum_width(
    tmp_path, script_format
):
    _real(script_format)
    run = _run_real(
        tmp_path,
        script_format,
        sequential={"clock_gating": {"enabled": True, "min_width": 5}},
    )
    assert "TINY_ICG " not in (run / "netlist.v").read_text()


@FORMATS_REAL
def test_real_yosys_retimes_the_selected_module(tmp_path, script_format):
    _real(script_format)
    run = _run_real(tmp_path, script_format, sequential={"retime_modules": ["en"]})
    log = (run / "yosys.log").read_text()
    assert "ABC: + retime -M 5 -v -o" in log
    assert "Found matching posedge clock domain" in log


@FORMATS_REAL
def test_real_yosys_takes_the_tristate_pass(tmp_path, script_format):
    _real(script_format)
    run = _run_real(tmp_path, script_format, sequential={"tristate": "logic"})
    assert "Executing TRIBUF pass" in (run / "yosys.log").read_text()


@FORMATS_REAL
def test_real_yosys_takes_no_latch_map_and_no_clock_gate_file(tmp_path, script_format):
    _real(script_format)
    _run_real(
        tmp_path,
        script_format,
        sequential={"latch_map": "none", "clockgate_map": "none"},
    )


@FORMATS_REAL
def test_real_yosys_reads_a_dff_map_at_a_path_with_a_space_and_pattern_characters(
    tmp_path, script_format
):
    _real(script_format)
    maps = tmp_path / "design" / "my maps"
    maps.mkdir(parents=True)
    (maps / "dffs [1].v").write_text(DFF_MAP)
    run = _run_real(tmp_path, script_format, sequential={"dff_map": "my maps/dffs [1].v"})
    assert "DFF_X1" in (run / "netlist.v").read_text()


def _run_mac(tmp_path: Path, script_format: str, **given: Any):
    """Run `yosys` on the `mac` design, whose module `mul8` the flattening dissolves."""
    design = _design(tmp_path / "design")
    settings = tiny_settings(tiny_store(tmp_path), clock={"period": "2 ns"}, **given)
    with contextlib.suppress(FlowException), forced_script_format(script_format):
        DefaultRunner(tmp_path / "xeda_run").run_flow(Yosys, design, settings)
    (results,) = tmp_path.glob("xeda_run/mac/yosys/results.json")
    assert (results.parent / f"yosys_synth.{script_format}").is_file()
    return results.parent, json.loads(results.read_text())


@FORMATS_REAL
def test_real_yosys_stops_when_a_retimed_module_is_gone_by_the_retiming(tmp_path, script_format):
    """The flattening dissolves `mul8`, and the assertion names it in the error."""
    _real(script_format)
    run, results = _run_mac(tmp_path, script_format, sequential={"retime_modules": ["mul8"]})
    assert results["outcome"] == "failed"
    log = (run / "yosys.log").read_text()
    error = log.rsplit("ERROR", 1)[-1]
    assert "ERROR" in log and "mul8" in error
    assert "Executing ABC pass" not in error


@FORMATS_REAL
def test_real_yosys_retimes_a_module_that_a_cell_attribute_keeps(tmp_path, script_format):
    _real(script_format)
    run, results = _run_mac(
        tmp_path,
        script_format,
        sequential={"retime_modules": ["mul8"]},
        set_attribute={"keep_hierarchy": {"mac/m": 1}},
    )
    assert results["outcome"] == "passed", results
    assert "ABC: + retime" in (run / "yosys.log").read_text()
