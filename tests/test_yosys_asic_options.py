"""The hierarchy, coarse, arithmetic and memory options of the `yosys` ASIC synthesis.

Most of these settings are options of `synth`, which the script runs in two parts: the coarse
part (`-run :fine`) and the fine part (`-run fine: -noabc`). An option that acts on the coarse part
goes on the first `synth` command only. The other settings are flags of the commands that the
script runs itself (`proc`, `opt`, `extract_fa`), or commands of their own (`scratchpad`,
`setattr`, `memory_libmap`, `techmap`). Every rendering test runs for both script formats. The Tcl
script runs under `tclsh` with every yosys command recording itself. The tests at the end run a
real yosys on the `tiny` platform. The settings that need Tcl are in
`tests/test_yosys_asic_tcl.py`.
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

from .test_yosys_asic import SCRIPT_FORMATS, _design, _tiny, _words
from .test_yosys_asic_groups import no_liberty_message, _refused, _run
from .tool_utils import require_yosys, require_yosys_tcl
from .yosys_format import forced_script_format

#: The default commands that the new settings change, as `_run` returns them.
DEFAULT_COARSE = ["synth", "-flatten", "-run", ":fine"]
DEFAULT_FINE = ["synth", "-top", "mac", "-run", "fine:", "-noabc"]

# ------------------------------------------------------------------------------ the model


def test_the_new_leaves_have_the_defaults_that_keep_the_script_as_it_was():
    settings = Yosys.Settings()
    assert settings.hierarchy.keep_instances == []
    assert settings.hierarchy.separator is None
    assert settings.hierarchy.cross_boundary_opt is False
    assert settings.coarse.fsm is True
    assert settings.coarse.share is True
    assert settings.coarse.latches is None
    assert settings.coarse.opt_dff_sat is False
    assert settings.arithmetic.alumacc is True
    assert settings.arithmetic.multiplier == "default"
    assert settings.arithmetic.arith_tree is False
    fa = settings.arithmetic.extract_fa
    assert (fa.full, fa.half, fa.max_depth, fa.max_breadth) == (True, True, None, None)
    assert settings.arithmetic.wrapped_adders == []
    assert settings.arithmetic.wrapped_multipliers == []
    assert settings.memory.libmap == []
    assert settings.memory.techmap is None
    assert settings.memory.max_bits is None
    assert settings.memory.mock_large is False
    assert settings.memory.keep_mocked is True
    assert settings.memory.merge_ff is True
    assert settings.memory.rw_check is True


def test_the_closed_names_of_the_new_leaves():
    arithmetic = asic_groups.ArithmeticGroup.model_fields
    assert set(get_args(arithmetic["multiplier"].annotation)) == {"default", "booth"}
    assert set(
        get_args(asic_groups.CoarseGroup.model_fields["latches"].annotation)[0].__args__
    ) == {
        "warn",
        "error",
    }
    for field, names in (
        ("wrapped_adders", {"brent_kung", "han_carlson", "kogge_stone", "sklansky"}),
        ("wrapped_multipliers", {"booth", "base"}),
    ):
        (item,) = get_args(arithmetic[field].annotation)
        assert set(get_args(item)) == names, field


@pytest.mark.parametrize(
    "given,mentions",
    [
        ({"arithmetic": {"wrapped_adders": ["ripple"]}}, "wrapped_adders"),
        ({"arithmetic": {"multiplier": "booth_lowpower"}}, "multiplier"),
        ({"coarse": {"latches": "info"}}, "latches"),
        ({"hierarchy": {"separator": ".."}}, "separator"),
        ({"hierarchy": {"separator": ""}}, "separator"),
        ({"hierarchy": {"separator": " "}}, "separator"),
        ({"memory": {"max_bits": 0}}, "max_bits"),
        ({"arithmetic": {"extract_fa": {"max_depth": 0}}}, "max_depth"),
        ({"arithmetic": {"extract_fa": {"max_breadth": 0}}}, "max_breadth"),
        ({"coarse": {"peepopt": False}}, "peepopt"),
        ({"coarse": {"wreduce": False}}, "wreduce"),
        ({"coarse": {"purge": False}}, "purge"),
        ({"coarse": {"fsm": {"enabled": False}}}, "fsm"),
        ({"hierarchy": {"uniquify": True}}, "uniquify"),
    ],
)
def test_a_leaf_refuses_a_value_outside_its_type_and_a_row_not_taken_is_no_setting(given, mentions):
    with pytest.raises(Exception, match=mentions):
        Yosys.Settings.from_input(given)


def test_a_list_leaf_accepts_comma_separated_text_and_refuses_brackets():
    settings = Yosys.Settings.from_input(
        {
            "arithmetic": {"wrapped_adders": "kogge_stone,sklansky"},
            "memory": {"libmap": "a.txt,b.txt"},
        }
    )
    assert settings.arithmetic.wrapped_adders == ["kogge_stone", "sklansky"]
    assert settings.memory.libmap == [Path("a.txt"), Path("b.txt")]
    with pytest.raises(Exception, match="wrapped_adders"):
        Yosys.Settings.from_input({"arithmetic": {"wrapped_adders": "[kogge_stone]"}})


def test_the_new_leaves_have_a_tier_and_a_description():
    for name in ("hierarchy", "coarse", "arithmetic", "memory"):
        group = Yosys.Settings.model_fields[name].annotation
        for leaf, info in group.model_fields.items():
            assert info.description, f"{name}.{leaf}"
            if leaf != "extract_fa":
                assert tier_of(group, leaf) is not None, f"{name}.{leaf}"
    fa = asic_groups.ExtractFaGroup
    for leaf, info in fa.model_fields.items():
        assert info.description and tier_of(fa, leaf) is not None, leaf


# ------------------------------------------------------------------------------ the hierarchy


def _kept(commands: list[list[str]]) -> list[list[str]]:
    """The `setattr` commands that set the `keep_hierarchy` attribute."""
    return [call for call in _words(commands, "setattr") if "keep_hierarchy" in call]


@SCRIPT_FORMATS
def test_the_default_hierarchy_settings_add_no_command(tmp_path, monkeypatch, script_format):
    commands = _run(tmp_path, monkeypatch, script_format)
    assert _words(commands, "scratchpad") == []
    assert _kept(commands) == []
    assert _words(commands, "synth")[0] == DEFAULT_COARSE
    assert _words(commands, "synth")[1] == DEFAULT_FINE
    assert _words(commands, "proc") == [["proc"]]


@SCRIPT_FORMATS
def test_the_separator_is_set_in_the_scratchpad_before_the_design_is_flattened(
    tmp_path, monkeypatch, script_format
):
    """`synth -flatten` has no option for the separator, so the scratchpad variable of `flatten`
    carries it. It reaches the `flatten` command and the one inside `synth`."""
    commands = _run(tmp_path, monkeypatch, script_format, hierarchy={"separator": "/"})
    assert _words(commands, "scratchpad") == [["scratchpad", "-set", "flatten.separator", "/"]]
    assert commands.index(["scratchpad", "-set", "flatten.separator", "/"]) < commands.index(
        ["flatten"]
    )


@SCRIPT_FORMATS
def test_kept_instances_get_the_keep_hierarchy_attribute_before_the_design_is_flattened(
    tmp_path, monkeypatch, script_format
):
    commands = _run(
        tmp_path, monkeypatch, script_format, hierarchy={"keep_instances": ["mac/m", "mac/u*"]}
    )
    attributes = [
        ["setattr", "-set", "keep_hierarchy", "1", "mac/m"],
        ["setattr", "-set", "keep_hierarchy", "1", "mac/u*"],
    ]
    assert _kept(commands) == attributes
    start = commands.index(["proc"])
    assert commands[start + 1 : start + 3] == attributes
    assert commands[start + 3] == ["flatten"]


@SCRIPT_FORMATS
def test_cross_boundary_optimization_is_the_hieropt_option_of_both_synth_commands(
    tmp_path, monkeypatch, script_format
):
    commands = _run(tmp_path, monkeypatch, script_format, hierarchy={"cross_boundary_opt": True})
    assert _words(commands, "synth") == [[*DEFAULT_COARSE, "-hieropt"], [*DEFAULT_FINE, "-hieropt"]]


# ------------------------------------------------------------------------------ coarse and memory

#: A setting and the option of the coarse `synth` command that it renders.
COARSE_OPTIONS = [
    ({"coarse": {"fsm": False}}, "-nofsm"),
    ({"coarse": {"share": False}}, "-noshare"),
    ({"arithmetic": {"alumacc": False}}, "-noalumacc"),
    ({"arithmetic": {"multiplier": "booth"}}, "-booth"),
    ({"arithmetic": {"arith_tree": True}}, "-arith_tree"),
    ({"memory": {"merge_ff": False}}, "-nordff"),
    ({"memory": {"rw_check": False}}, "-no-rw-check"),
]


@SCRIPT_FORMATS
@pytest.mark.parametrize(
    "given,option", COARSE_OPTIONS, ids=[option for _, option in COARSE_OPTIONS]
)
def test_a_coarse_option_goes_on_the_first_synth_command_only(
    tmp_path, monkeypatch, script_format, given, option
):
    commands = _run(tmp_path, monkeypatch, script_format, **given)
    assert _words(commands, "synth") == [[*DEFAULT_COARSE, option], DEFAULT_FINE]


@SCRIPT_FORMATS
def test_the_coarse_options_come_before_the_extra_map_and_the_synth_flags(
    tmp_path, monkeypatch, script_format
):
    commands = _run(
        tmp_path,
        monkeypatch,
        script_format,
        coarse={"fsm": False, "share": False},
        arithmetic={"adder_architecture": "sklansky", "multiplier": "booth"},
        hierarchy={"cross_boundary_opt": True},
        synth_flags=["-nowidelut"],
    )
    extra = ["-extra-map", "+/choices/sklansky.v", "-nowidelut"]
    assert _words(commands, "synth") == [
        [*DEFAULT_COARSE, "-nofsm", "-noshare", "-booth", "-hieropt", *extra],
        [*DEFAULT_FINE, "-hieropt", *extra],
    ]


@SCRIPT_FORMATS
@pytest.mark.parametrize("mode", ["warn", "error"])
def test_latches_is_the_option_of_the_proc_command_that_the_script_runs(
    tmp_path, monkeypatch, script_format, mode
):
    """The script runs `proc` itself, so the `proc` inside `synth` finds no process: the
    option goes on the command that infers the latches."""
    commands = _run(tmp_path, monkeypatch, script_format, coarse={"latches": mode})
    assert _words(commands, "proc") == [["proc", "-latches", mode]]
    assert all("-latches" not in synth for synth in _words(commands, "synth"))


@SCRIPT_FORMATS
def test_opt_dff_sat_adds_the_sat_option_to_the_opt_command_before_the_mapping(
    tmp_path, monkeypatch, script_format
):
    commands = _run(tmp_path, monkeypatch, script_format, coarse={"opt_dff_sat": True})
    assert _words(commands, "opt") == [
        ["opt", "-purge", "-sat"],
        ["opt", "-fast", "-purge"],
        ["opt"],
    ]


@SCRIPT_FORMATS
@pytest.mark.parametrize(
    "given,words",
    [
        ({"full": False}, ["-ha"]),
        ({"half": False}, ["-fa"]),
        ({"max_depth": 10}, ["-d", "10", "-fa", "-ha"]),
        ({"max_breadth": 3}, ["-b", "3", "-fa", "-ha"]),
        ({"half": False, "max_depth": 8, "max_breadth": 4}, ["-d", "8", "-b", "4", "-fa"]),
    ],
)
def test_extract_fa_takes_the_limits_before_the_kinds(
    tmp_path, monkeypatch, script_format, given, words
):
    commands = _run(tmp_path, monkeypatch, script_format, arithmetic={"extract_fa": given})
    assert _words(commands, "extract_fa") == [["extract_fa", *words]]


@SCRIPT_FORMATS
def test_no_adder_map_means_no_extract_fa(tmp_path, monkeypatch, script_format):
    commands = _run(tmp_path, monkeypatch, script_format, arithmetic={"adder_map": "none"})
    assert _words(commands, "extract_fa") == []


@SCRIPT_FORMATS
def test_the_memory_libraries_and_the_techmap_run_between_the_two_synth_commands(
    tmp_path, monkeypatch, script_format
):
    """`memory_libmap` maps the memories that the coarse part collected, and the fine part maps
    the others to flip-flops."""
    design = tmp_path / "design"
    design.mkdir()
    for name in ("one.txt", "two.txt", "mem_map.v"):
        (design / name).write_text("\n")
    commands = _run(
        tmp_path,
        monkeypatch,
        script_format,
        memory={"libmap": ["one.txt", "two.txt"], "techmap": "mem_map.v"},
    )
    libmap = ["memory_libmap", "-lib", str(design / "one.txt"), "-lib", str(design / "two.txt")]
    techmap = ["techmap", "-map", str(design / "mem_map.v")]
    first, second = (commands.index(synth) for synth in _words(commands, "synth"))
    assert commands[first + 1 : first + 3] == [libmap, techmap]
    assert second == first + 3


@SCRIPT_FORMATS
def test_the_default_memory_settings_run_no_memory_command(tmp_path, monkeypatch, script_format):
    commands = _run(tmp_path, monkeypatch, script_format)
    assert _words(commands, "memory_libmap") == []
    assert [call for call in commands if call[:2] == ["techmap", "-map"]] != []
    maps = [call[2] for call in commands if call[:2] == ["techmap", "-map"]]
    assert all(Path(path).name.startswith("cells_") for path in maps)


# ------------------------------------------------------------------------------ the refusals


def _keys(**given: Any) -> dict[str, str]:
    return dict(_refused(platform="nangate45", **given))


def test_instances_and_a_separator_need_a_design_that_is_flattened():
    problems = _keys(flatten=False, hierarchy={"keep_instances": ["a/b"], "separator": "/"})
    for key in ("hierarchy.keep_instances", "hierarchy.separator"):
        assert "`flatten: false`" in problems[key] and f"`{key}`" in problems[key], key


def test_instances_with_flatten_true_or_unset_are_accepted():
    for flatten in (True, None):
        Yosys.check_settings_supported(
            Yosys.Settings(
                platform="nangate45",
                flatten=flatten,
                hierarchy={"keep_instances": ["a/b"], "separator": "/"},
            )
        )


def test_adder_architecture_needs_alumacc():
    problems = _keys(arithmetic={"adder_architecture": "sklansky", "alumacc": False})
    message = problems["arithmetic.adder_architecture"]
    assert "`arithmetic.alumacc: false`" in message and "`$alu`" in message
    Yosys.check_settings_supported(
        Yosys.Settings(platform="nangate45", arithmetic={"alumacc": False})
    )


def test_a_booth_multiplier_cannot_be_chosen_when_the_operators_are_wrapped():
    problems = _keys(
        arithmetic={"multiplier": "booth", "wrap_operators": True}, abc={"mapper": "abc_new"}
    )
    message = problems["arithmetic.multiplier"]
    assert "`arithmetic.wrapped_multipliers`" in message and "wrap_operators" in message


@pytest.mark.parametrize(
    "given",
    [{"max_depth": 10}, {"max_breadth": 3}, {"full": False}, {"half": False}],
)
def test_extract_fa_settings_have_no_effect_without_an_adder_map(given):
    problems = _keys(arithmetic={"adder_map": "none", "extract_fa": given})
    (key,) = (key for key in problems if key.startswith("arithmetic.extract_fa."))
    assert f"`arithmetic.extract_fa.{next(iter(given))}`" in problems[key]
    assert "`arithmetic.adder_map: none`" in problems[key]


def test_extract_fa_needs_one_kind_of_adder():
    problems = _keys(arithmetic={"extract_fa": {"full": False, "half": False}})
    message = problems["arithmetic.extract_fa.full"]
    assert "`arithmetic.extract_fa.half`" in message


def test_the_wrapped_architectures_need_wrapping():
    problems = _keys(
        arithmetic={"wrapped_adders": ["sklansky"], "wrapped_multipliers": ["booth", "base"]}
    )
    for key in ("arithmetic.wrapped_adders", "arithmetic.wrapped_multipliers"):
        assert f"`{key}`" in problems[key] and "`arithmetic.wrap_operators`" in problems[key]


@pytest.mark.parametrize(
    "key,names",
    [
        ("wrapped_adders", ["kogge_stone", "sklansky", "kogge_stone"]),
        ("wrapped_multipliers", ["base", "base"]),
    ],
)
def test_a_wrapped_architecture_is_named_once(key, names):
    problems = _keys(arithmetic={key: names, "wrap_operators": True}, abc={"mapper": "abc_new"})
    message = problems[f"arithmetic.{key}"]
    assert f"`{names[0] if key == 'wrapped_multipliers' else 'kogge_stone'}`" in message
    assert "more than once" in message


def test_wrapped_architectures_in_order_are_accepted():
    Yosys.check_settings_supported(
        Yosys.Settings(
            platform="nangate45",
            abc={"mapper": "abc_new"},
            arithmetic={
                "wrap_operators": True,
                "wrapped_adders": ["sklansky", "brent_kung"],
                "wrapped_multipliers": ["base"],
            },
        )
    )


def test_mocking_needs_a_limit_to_mock_above():
    problems = _keys(memory={"mock_large": True})
    message = problems["memory.mock_large"]
    assert "`memory.max_bits`" in message


def test_keep_mocked_false_needs_mocking():
    problems = _keys(memory={"max_bits": 64, "keep_mocked": False})
    message = problems["memory.keep_mocked"]
    assert "`memory.mock_large`" in message
    Yosys.check_settings_supported(
        Yosys.Settings(
            platform="nangate45",
            memory={"max_bits": 64, "mock_large": True, "keep_mocked": False},
        )
    )


@pytest.mark.parametrize(
    "flag,key",
    [
        ("-nofsm", "coarse.fsm"),
        ("-noshare", "coarse.share"),
        ("-noalumacc", "arithmetic.alumacc"),
        ("-booth", "arithmetic.multiplier"),
        ("-arith_tree", "arithmetic.arith_tree"),
        ("-nordff", "memory.merge_ff"),
        ("-no-rw-check", "memory.rw_check"),
        ("-hieropt", "hierarchy.cross_boundary_opt"),
        ("-latches warn", "coarse.latches"),
    ],
)
def test_a_synth_flag_that_a_setting_owns_is_refused_naming_the_setting(flag, key):
    problems = _keys(synth_flags=[flag])
    option = flag.split()[0]
    assert f"`{key}`" in problems["synth_flags"] and f"`{option}`" in problems["synth_flags"]


def test_other_synth_flags_are_accepted():
    Yosys.check_settings_supported(
        Yosys.Settings(platform="nangate45", synth_flags=["-relativeshare", "-nowidelut"])
    )


@pytest.mark.parametrize(
    "key,given",
    [
        ("hierarchy.keep_instances", {"hierarchy": {"keep_instances": ["a/b"]}}),
        ("hierarchy.separator", {"hierarchy": {"separator": "/"}}),
        ("hierarchy.cross_boundary_opt", {"hierarchy": {"cross_boundary_opt": True}}),
        ("coarse.fsm", {"coarse": {"fsm": False}}),
        ("coarse.share", {"coarse": {"share": False}}),
        ("coarse.latches", {"coarse": {"latches": "warn"}}),
        ("coarse.opt_dff_sat", {"coarse": {"opt_dff_sat": True}}),
        ("arithmetic.alumacc", {"arithmetic": {"alumacc": False}}),
        ("arithmetic.multiplier", {"arithmetic": {"multiplier": "booth"}}),
        ("arithmetic.arith_tree", {"arithmetic": {"arith_tree": True}}),
        ("arithmetic.extract_fa.max_depth", {"arithmetic": {"extract_fa": {"max_depth": 5}}}),
        (
            "arithmetic.wrapped_adders",
            {"arithmetic": {"wrap_operators": True, "wrapped_adders": ["sklansky"]}},
        ),
        ("memory.libmap", {"memory": {"libmap": ["m.txt"]}}),
        ("memory.techmap", {"memory": {"techmap": "m.v"}}),
        ("memory.max_bits", {"memory": {"max_bits": 8}}),
        ("memory.merge_ff", {"memory": {"merge_ff": False}}),
        ("memory.rw_check", {"memory": {"rw_check": False}}),
    ],
)
@pytest.mark.parametrize("mapping", [{"gates": ["AND"]}, {}], ids=str)
def test_a_new_leaf_is_refused_when_no_liberty_mapping_runs(key, given, mapping):
    problems = dict(_refused(**given, **mapping))
    assert no_liberty_message(key, mapping) == problems[key]


# ------------------------------------------------------------------------------ real yosys

#: Settings that are new to the script, each of which a real yosys must take.
REAL_YOSYS = [
    ({"hierarchy": {"keep_instances": ["mac/m"]}}, "keep_instances"),
    ({"hierarchy": {"separator": "/"}}, "separator"),
    ({"hierarchy": {"cross_boundary_opt": True}}, "cross_boundary_opt"),
    ({"hierarchy": {"cross_boundary_opt": True, "keep_instances": ["mac/m"]}}, "hieropt-kept"),
    ({"coarse": {"fsm": False}}, "no_fsm"),
    ({"coarse": {"share": False}}, "no_share"),
    ({"coarse": {"latches": "warn"}}, "latches_warn"),
    ({"coarse": {"latches": "error"}}, "latches_error"),
    ({"coarse": {"opt_dff_sat": True}}, "opt_dff_sat"),
    ({"arithmetic": {"alumacc": False}}, "no_alumacc"),
    ({"arithmetic": {"multiplier": "booth"}}, "booth"),
    ({"arithmetic": {"arith_tree": True}}, "arith_tree"),
    (
        {"arithmetic": {"extract_fa": {"half": False, "max_depth": 8, "max_breadth": 4}}},
        "extract_fa",
    ),
    ({"memory": {"merge_ff": False}}, "no_merge_ff"),
    ({"memory": {"rw_check": False}}, "no_rw_check"),
]


def _run_real(tmp_path: Path, given: dict[str, Any], script_format: str) -> None:
    design = _design(tmp_path)
    settings = _tiny(tmp_path, **given)
    with contextlib.suppress(FlowException), forced_script_format(script_format):
        DefaultRunner(tmp_path / "xeda_run").run_flow(Yosys, design, settings)
    (results,) = tmp_path.glob("xeda_run/mac/yosys/results.json")
    assert json.loads(results.read_text())["outcome"] == "passed"
    assert (results.parent / f"yosys_synth.{script_format}").is_file()


@pytest.mark.parametrize(
    "given", [given for given, _ in REAL_YOSYS], ids=[i for _, i in REAL_YOSYS]
)
def test_real_yosys_takes_each_new_setting(tmp_path, given):
    require_yosys()
    _run_real(tmp_path, given, "ys")


@pytest.mark.parametrize(
    "given", [given for given, _ in REAL_YOSYS], ids=[i for _, i in REAL_YOSYS]
)
def test_real_yosys_with_tcl_takes_each_new_setting(tmp_path, given):
    require_yosys_tcl()
    _run_real(tmp_path, given, "tcl")


@pytest.mark.parametrize("separator,joined", [(None, r"\m.a "), ("/", r"\m/a ")])
def test_real_yosys_flattens_with_the_separator_of_the_setting(tmp_path, separator, joined):
    """The RTL output comes after `flatten`, so it names the flattened wires."""
    require_yosys()
    design = _design(tmp_path)
    hierarchy = {} if separator is None else {"separator": separator}
    given = _tiny(tmp_path, hierarchy=hierarchy, rtl_verilog="rtl.v")
    flow = DefaultRunner(tmp_path / "xeda_run").run_flow(Yosys, design, given)
    assert flow.succeeded
    assert joined in (flow.run_path / "rtl.v").read_text()


def test_real_yosys_keeps_an_instance_it_is_told_to_keep(tmp_path):
    require_yosys()
    design = _design(tmp_path)
    given = _tiny(tmp_path, hierarchy={"keep_instances": ["mac/m"]})
    flow = DefaultRunner(tmp_path / "xeda_run").run_flow(Yosys, design, given)
    assert flow.succeeded
    netlist = (flow.run_path / "netlist.v").read_text()
    assert re.search(r"^module mul8\b", netlist, re.M)


RAM = (
    "module ram16(input clk, input we, input [3:0] addr, input [7:0] din, output [7:0] dout);\n"
    "  reg [7:0] mem [0:15];\n"
    "  always @(posedge clk) if (we) mem[addr] <= din;\n"
    "  assign dout = mem[addr];\n"
    "endmodule\n"
)

#: A library of one distributed RAM, 16 words of 8 bits with a write port that also reads.
MEMORY_LIBRARY = """\
ram distributed $__MYRAM {
  abits 4;
  width 8;
  cost 3;
  init no_undef;
  prune_rom;
  port arsw "RW" {
    clock posedge;
  }
}
"""

#: The map of that RAM to flip-flops of yosys, one per bit.
MEMORY_MAP = """\
module $__MYRAM (...);
  parameter INIT = 0;
  output [7:0] PORT_RW_RD_DATA;
  input [7:0] PORT_RW_WR_DATA;
  input [3:0] PORT_RW_ADDR;
  input PORT_RW_WR_EN;
  input PORT_RW_CLK;
  wire [127:0] q;
  genvar i, j;
  generate
    for (i = 0; i < 16; i = i + 1) begin : row
      wire sel = PORT_RW_WR_EN && PORT_RW_ADDR == i;
      for (j = 0; j < 8; j = j + 1) begin : col
        \\$_DFFE_PP_ ff (.C(PORT_RW_CLK), .D(PORT_RW_WR_DATA[j]), .E(sel), .Q(q[i * 8 + j]));
      end
    end
  endgenerate
  assign PORT_RW_RD_DATA = q[PORT_RW_ADDR * 8 +: 8];
endmodule
"""


def test_real_yosys_maps_a_memory_with_the_library_and_the_techmap_of_the_settings(tmp_path):
    require_yosys()
    root = tmp_path / "design"
    root.mkdir()
    (root / "ram.v").write_text(RAM)
    (root / "mem.txt").write_text(MEMORY_LIBRARY)
    (root / "mem_map.v").write_text(MEMORY_MAP)
    design = _design_from(root, "ram16")
    given = _tiny(tmp_path, memory={"libmap": ["mem.txt"], "techmap": "mem_map.v"})
    flow = DefaultRunner(tmp_path / "xeda_run").run_flow(Yosys, design, given)
    assert flow.succeeded
    log = (flow.run_path / "yosys.log").read_text()
    assert "mapping memory ram16.mem via $__MYRAM" in log


def _design_from(root: Path, top: str) -> Design:
    return Design(
        name=top,
        design_root=root,
        rtl={"sources": ["ram.v"], "top": top, "clock": {"port": "clk"}},
    )


def test_both_extract_kinds_off_with_the_adder_map_off_gives_one_message_per_leaf():
    """Both kinds false while the map is off: each leaf is told that it has no effect, and none
    is told to set what is already set."""
    messages = [
        message
        for key, message in _refused(
            platform="nangate45",
            arithmetic={"adder_map": "none", "extract_fa": {"full": False, "half": False}},
        )
        if key.startswith("arithmetic.extract_fa.")
    ]
    assert len(messages) == 2
    assert all("has no effect with `arithmetic.adder_map: none`" in m for m in messages)
    assert not any("set `arithmetic.adder_map: none`" in m for m in messages)


def test_both_extract_kinds_off_with_the_adder_map_on_gives_one_message():
    messages = [
        message
        for key, message in _refused(
            platform="nangate45",
            arithmetic={"extract_fa": {"full": False, "half": False}},
        )
        if key.startswith("arithmetic.")
    ]
    assert len(messages) == 1
    for word in ("extract_fa.full", "extract_fa.half", "set `arithmetic.adder_map: none`"):
        assert word in messages[0], messages[0]


def test_the_extract_fa_descriptions_say_when_the_adder_map_runs():
    from xeda.flows.yosys.asic_groups import ArithmeticGroup, ExtractFaGroup

    for description in (
        ArithmeticGroup.model_fields["extract_fa"].description,
        ExtractFaGroup.__doc__,
    ):
        assert description and "operator wrapping" in description and "no adder map" in description
    assert "operator wrapping" in ArithmeticGroup.model_fields["adder_map_file"].description


@pytest.mark.parametrize("architecture", ["kogge_stone", "han_carlson", "sklansky"])
def test_an_adder_architecture_is_refused_with_operator_wrapping(architecture):
    """Wrapping replaces the fine part of `synth`, where `-extra-map` acts, so the
    architecture would do nothing. The wrapped adders choose it."""
    messages = [
        message
        for key, message in _keys_all(
            arithmetic={"adder_architecture": architecture, "wrap_operators": True},
            abc={"mapper": "abc_new"},
        )
        if key == "arithmetic.adder_architecture"
    ]
    assert len(messages) == 1
    for word in ("`arithmetic.adder_architecture", "`arithmetic.wrapped_adders`"):
        assert word in messages[0], messages[0]
    Yosys.check_settings_supported(
        Yosys.Settings(
            platform="nangate45",
            arithmetic={"wrap_operators": True, "wrapped_adders": [architecture]},
            abc={"mapper": "abc_new"},
        )
    )


def _keys_all(**given: Any) -> list[tuple[str, str]]:
    return _refused(platform="nangate45", **given)


def test_the_descriptions_name_the_synth_options_that_wrapping_leaves_out():
    from xeda.flows.yosys.asic_groups import HierarchyGroup

    assert "wrap_operators" in HierarchyGroup.model_fields["cross_boundary_opt"].description
    assert "wrap_operators" in Yosys.Settings.model_fields["synth_flags"].description
