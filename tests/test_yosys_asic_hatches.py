"""The escape hatches of the `yosys` ASIC synthesis: lists of flags for one command.

A hatch passes extra flags to a command whose option set is larger than the typed settings. It
refuses every flag that a setting renders or that the script fixes, and it names that setting.
One table per command holds those flags, and one check reads all the tables. The tests check
every word of every item, the flags that stay free, and the tables against what the scripts
render.
"""

import re
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda.flow import FlowSettingsError
from xeda.flow_runner import DefaultRunner
from xeda.flows import Yosys, YosysFpga, YosysSim
from xeda.flows.yosys import flag_hatches
from xeda.flows.yosys.asic_groups import AbcGroup

from .test_yosys_asic import SCRIPT_FORMATS, _words
from .test_yosys_asic_groups import _refused, _run
from .test_yosys import _probe_yosys_slang, _require
from .test_yosys_templates import _render
from .tool_utils import require_yosys


def _problems(**given: Any) -> list[tuple[str, str]]:
    return _refused(platform="nangate45", **given)


def _owned(hatch: str, item: str) -> list[str]:
    """The messages that `item` in `hatch` gets: none when the check accepts it."""
    settings = Yosys.Settings.from_input({"platform": "nangate45", hatch: [item]})
    try:
        Yosys.check_settings_supported(settings)
    except FlowSettingsError as refused:
        return [message for key, message, *_ in refused.errors if key == hatch]
    return []


# ------------------------------------------------------------------------------ the tables

#: A flag of each hatch, the setting that owns it and the form of the flag that the user writes.
SETTING_OWNED = [
    ("synth_flags", "-nofsm", "coarse.fsm"),
    ("synth_flags", "-noshare", "coarse.share"),
    ("synth_flags", "-noalumacc", "arithmetic.alumacc"),
    ("synth_flags", "-booth", "arithmetic.multiplier"),
    ("synth_flags", "-arith_tree", "arithmetic.arith_tree"),
    ("synth_flags", "-nordff", "memory.merge_ff"),
    ("synth_flags", "-no-rw-check", "memory.rw_check"),
    ("synth_flags", "-hieropt", "hierarchy.cross_boundary_opt"),
    ("synth_flags", "-latches warn", "coarse.latches"),
    ("synth_flags", "-flatten", "flatten"),
    ("synth_flags", "-extra-map +/choices/sklansky.v", "arithmetic.adder_architecture"),
    ("abc_flags", "-keepff", "abc.keep_ff"),
    ("abc_flags", "-dress", "abc.dress"),
    ("abc_flags", "-liberty_args x", "abc.liberty_args"),
    ("abc_flags", "-D 100", "abc.delay_target"),
    ("abc_flags", "-script x", "abc_script"),
    ("abc_flags", "-constr x", "abc_constr"),
    ("abc_flags", "-liberty x", "liberty"),
    ("abc_flags", "-dont_use x", "dont_use_cells"),
    ("abc_flags", "-dff", "abc_dff"),
    ("abc_flags", "-g AND", "gates"),
    ("abc_flags", "-lut 4", "lut"),
    ("abc_flags", "-luts 4", "lut"),
    ("read_verilog_flags", "-DNAME=1", "defines"),
    ("read_verilog_flags", "-D NAME", "defines"),
    ("netlist_verilog_flags", "-nodec", "netlist_dec"),
    ("netlist_verilog_flags", "-nohex", "netlist_hex"),
    ("netlist_verilog_flags", "-noexpr", "netlist_expr"),
    ("netlist_verilog_flags", "-noattr", "netlist_attrs"),
    ("netlist_verilog_flags", "-blackboxes", "netlist_blackboxes"),
    ("netlist_verilog_flags", "-simple-lhs", "netlist_simple_lhs"),
]

#: A flag that the script fixes, with a word of the reason that the message gives.
SCRIPT_FIXED = [
    ("synth_flags", "-top x", "rtl.top"),
    ("synth_flags", "-noabc", "`abc`"),
    ("synth_flags", "-run :fine", "two parts"),
    ("synth_flags", "-lut 4", "LUT"),
    ("read_verilog_flags", "-defer", "-defer"),
    ("read_verilog_flags", "-Iinclude", "header"),
    ("read_verilog_flags", "-I include", "header"),
]


@pytest.mark.parametrize("hatch,item,owner", SETTING_OWNED)
def test_a_flag_that_a_setting_owns_is_refused_naming_the_setting(hatch, item, owner):
    word = item.split()[0]
    (message,) = _owned(hatch, item)
    assert message == f"`{word}` in `{hatch}` is the setting `{owner}`: set that instead"


@pytest.mark.parametrize("hatch,item,reason", SCRIPT_FIXED)
def test_a_flag_that_the_script_fixes_is_refused_saying_so(hatch, item, reason):
    word = item.split()[0]
    (message,) = _owned(hatch, item)
    assert message.startswith(f"`{word}` in `{hatch}` is fixed: ")
    assert reason in message and message.endswith(". Delete it")


@pytest.mark.parametrize("hatch,item,owner", SETTING_OWNED)
def test_every_word_of_an_item_is_checked(hatch, item, owner):
    """An item may hold several words: the owned flag is refused wherever it stands."""
    flag = item.split()[0]
    messages = _owned(hatch, f"-showtmp {item}")
    assert len(messages) == 1 and f"`{owner}`" in messages[0], flag
    messages = _owned(hatch, f"{item} -showtmp")
    assert len(messages) == 1 and f"`{owner}`" in messages[0], flag


def test_an_item_with_two_owned_flags_gets_one_message_for_each():
    messages = _owned("synth_flags", "-nowidelut -nofsm -noshare")
    assert len(messages) == 2
    assert "`-nofsm`" in messages[0] and "`-noshare`" in messages[1]


@pytest.mark.parametrize(
    "hatch,items",
    [
        ("synth_flags", ["-relativeshare", "-nowidelut", "-encfile x", "-auto-top"]),
        ("abc_flags", ["-nocleanup", "-showtmp", "-markgroups", "-exe x", "-fast"]),
        ("read_verilog_flags", ["-noautowire", "-sv", "-nolatches", "-formal"]),
        ("netlist_verilog_flags", ["-defparam", "-extmem", "-norename", "-sv", "-selected"]),
    ],
)
def test_the_other_flags_stay_free(hatch, items):
    assert _owned(hatch, " ".join(items)) == []
    for item in items:
        assert _owned(hatch, item) == [], item


@pytest.mark.parametrize("word", ["5", "x", "100"])
def test_a_value_is_no_flag(word):
    """A word that does not start with a dash is the value of a flag, whatever it spells."""
    assert _owned("abc_flags", f"-exe {word}") == []


@pytest.mark.parametrize(
    "item",
    ["-DFOO", "-DFOO=1", "-Iinc", "-I/some/dir"],
)
def test_a_one_letter_flag_of_read_verilog_may_hold_its_value(item):
    assert len(_owned("read_verilog_flags", item)) == 1


def test_a_long_flag_may_hold_its_value_after_an_equals_sign():
    owned = flag_hatches.owned_words("read_slang", ["--top=x", "--top x", "--topology"])
    assert [flag for _, flag, _ in owned] == ["--top", "--top"]


def test_the_hatches_are_checked_for_a_liberty_mapping_only():
    """The FPGA flows and the mappings to gates and LUTs keep their lists as they were."""
    for hatch, item in (("synth_flags", "-nofsm"), ("abc_flags", "-dff"), ("synth_flags", "-top")):
        Yosys.check_settings_supported(Yosys.Settings(gates=["AND"], **{hatch: [item]}))
    Yosys.check_settings_supported(Yosys.Settings(lut="4", abc_flags=["-script x"]))


def test_all_the_problems_come_back_in_one_error():
    settings = Yosys.Settings(
        platform="nangate45",
        synth_flags=["-nofsm"],
        abc_flags=["-dff"],
        read_verilog_flags=["-defer"],
        netlist_verilog_flags=["-nohex"],
    )
    with pytest.raises(FlowSettingsError) as refused:
        Yosys.check_settings_supported(settings)
    keys = [key for key, *_ in refused.value.errors]
    assert keys == ["synth_flags", "abc_flags", "read_verilog_flags", "netlist_verilog_flags"]


# ------------------------------------------------------------------------------ the tables agree


def _setting_exists(path: str) -> bool:
    node: Any = Yosys.Settings
    for part in path.split("."):
        fields = node.model_fields
        if part not in fields:
            return False
        node = fields[part].annotation
    return True


def test_every_owner_in_a_table_is_a_setting_or_a_key_of_the_design():
    for command, table in flag_hatches.OWNED_FLAGS.items():
        for flag, owner in table.items():
            if owner.setting is None:
                assert owner.reason, (command, flag)
            elif not owner.setting.startswith("rtl."):
                assert _setting_exists(owner.setting), (command, flag, owner.setting)


def test_the_flags_of_a_group_are_in_the_table_of_their_command():
    for flag in AbcGroup.FLAGS:
        assert flag_hatches.OWNED_FLAGS["abc"][flag.flag].setting == f"abc.{flag.field}"


def test_the_write_verilog_table_is_the_switch_table_of_the_netlist():
    from xeda.flows.yosys.common import YosysSynthesisBase

    table = flag_hatches.OWNED_FLAGS["write_verilog"]
    assert {flag: owner.setting for flag, owner in table.items()} == {
        flag: field for field, flag, _ in flag_hatches.WRITE_VERILOG_SWITCHES
    }
    code = YosysSynthesisBase.Settings.write_verilog_flags.__code__
    assert "WRITE_VERILOG_SWITCHES" in code.co_names


@pytest.mark.parametrize("field,flag,when", flag_hatches.WRITE_VERILOG_SWITCHES)
def test_each_switch_of_the_table_adds_its_flag_to_write_verilog(field, flag, when):
    """The table and `write_verilog_flags` agree: the switch at its value adds the flag, and at
    the other value does not."""
    for value, expected in ((when, True), (not when, False)):
        settings = Yosys.Settings.from_input({field: value})
        assert (flag in settings.write_verilog_flags()) is expected


def test_each_hatch_names_a_field_of_the_settings_and_a_table():
    for hatch in flag_hatches.HATCHES:
        assert hatch.setting in Yosys.Settings.model_fields
        assert hatch.command in flag_hatches.OWNED_FLAGS


def _flags_of(commands: list[list[str]], name: str) -> set[str]:
    return {
        word
        for command in _words(commands, name)
        for word in command
        if re.match(r"-[A-Za-z]", word)
    }


@SCRIPT_FORMATS
def test_every_flag_the_script_renders_for_synth_and_abc_is_in_a_table(
    tmp_path, monkeypatch, script_format
):
    """The script's own flags are the ones a hatch must refuse, so the tables hold them all."""
    commands = _run(
        tmp_path / "one",
        monkeypatch,
        script_format,
        coarse={"fsm": False, "share": False, "latches": "warn"},
        arithmetic={"adder_architecture": "sklansky", "multiplier": "booth", "arith_tree": True},
        memory={"merge_ff": False, "rw_check": False},
        hierarchy={"cross_boundary_opt": True},
        abc={"liberty_args": "-G 250"},
        abc_constr=["set_load 1"],
        netlist_attrs=False,
    ) + _run(
        tmp_path / "two",
        monkeypatch,
        script_format,
        arithmetic={"alumacc": False},
    )
    synth = _flags_of(commands, "synth") | _flags_of(commands, "proc")
    assert synth <= set(flag_hatches.OWNED_FLAGS["synth"]), synth
    abc = _flags_of(commands, "abc") - {"-G", "-G 250"}
    assert abc <= set(flag_hatches.OWNED_FLAGS["abc"]), abc
    write = _flags_of(commands, "write_verilog")
    assert write <= set(flag_hatches.OWNED_FLAGS["write_verilog"]), write
    for flag in ("-nofsm", "-noalumacc", "-latches", "-liberty_args", "-noattr"):
        assert flag in synth | abc | write, flag


# ------------------------------------------------------------------------------ the SystemVerilog reader

SLANG_OWNED = [
    "--top",
    "--top=x",
    "-D",
    "-DNAME=1",
    "-I",
    "-Iinclude",
    "-G",
    "-Gparam=1",
    "--blackboxed-module",
    "--extern-modules",
    "--best-effort-hierarchy",
]


def _sv_design(root: Path) -> Design:
    root.mkdir(parents=True, exist_ok=True)
    (root / "m.sv").write_text("module m; endmodule\n")
    return Design(name="m", design_root=root, rtl={"sources": ["m.sv"], "top": "m"})


@pytest.mark.parametrize("flag", SLANG_OWNED)
def test_a_flag_that_the_script_gives_to_slang_is_refused(flag):
    settings = Yosys.Settings(systemverilog="slang", read_systemverilog_flags=[flag])
    with pytest.raises(FlowSettingsError) as refused:
        Yosys.check_settings_supported(settings)
    (error,) = [e for e in refused.value.errors if e[0] == "read_systemverilog_flags"]
    assert flag.split("=")[0] in error[1] and "`read_systemverilog_flags`" in error[1]


@pytest.mark.parametrize("flow", [Yosys, YosysFpga, YosysSim])
def test_the_check_of_the_slang_flags_is_the_same_in_every_yosys_flow(flow):
    given: dict[str, Any] = {"systemverilog": "slang", "read_systemverilog_flags": ["--top=x"]}
    if flow is YosysFpga:
        given["fpga"] = {"part": "xc7a35tcpg236-1"}
    with pytest.raises(FlowSettingsError) as refused:
        flow.check_settings_supported(flow.Settings.from_input(given))
    assert [e[0] for e in refused.value.errors].count("read_systemverilog_flags") == 1


@pytest.mark.parametrize("front_end", ["default", "uhdm"])
def test_the_slang_flags_are_free_for_the_other_front_ends(front_end):
    Yosys.check_settings_supported(
        Yosys.Settings(
            systemverilog=front_end, read_systemverilog_flags=["--top=x", "-DNAME", "-G"]
        )
    )


def test_other_flags_of_slang_stay_free():
    Yosys.check_settings_supported(
        Yosys.Settings(
            systemverilog="slang",
            read_systemverilog_flags=[
                "--ignore-timing",
                "--allow-use-before-declare",
                "--compat=vcs",
            ],
        )
    )


@SCRIPT_FORMATS
def test_the_flags_reach_the_slang_reader_before_the_source(tmp_path, script_format):
    run = tmp_path / "run"
    run.mkdir()
    script = _render(
        Yosys,
        {
            "systemverilog": "slang",
            "gates": ["AND"],
            "script_format": script_format,
            "read_systemverilog_flags": ["--ignore-timing", "--compat=vcs"],
        },
        run,
        design=_sv_design(tmp_path / "design"),
    )
    (line,) = [line for line in script.splitlines() if "read_slang" in line]
    assert re.search(
        r"read_slang --extern-modules --best-effort-hierarchy\s+--ignore-timing --compat=vcs\s+.*m\.sv",
        line,
    ), line


@SCRIPT_FORMATS
def test_no_flags_leave_the_slang_line_as_it_was(tmp_path, script_format):
    run = tmp_path / "run"
    run.mkdir()
    script = _render(
        Yosys,
        {"systemverilog": "slang", "gates": ["AND"], "script_format": script_format},
        run,
        design=_sv_design(tmp_path / "design"),
    )
    (line,) = [line for line in script.splitlines() if "read_slang" in line]
    assert re.search(r'--best-effort-hierarchy\s+\S*m\.sv"?$', line), line


# ------------------------------------------------------------------------------ real yosys


def test_real_slang_takes_the_flags_of_the_hatch(tmp_path):
    """`read_slang` accepts a flag of its own through `read_systemverilog_flags`."""
    require_yosys()
    _require("the yosys slang plugin", _probe_yosys_slang(), "`plugin -i slang`")
    design = _sv_design(tmp_path / "design")
    flow = DefaultRunner(tmp_path / "xeda_run").run_flow(
        Yosys, design, {"read_systemverilog_flags": ["--ignore-timing"]}
    )
    assert flow is not None and flow.succeeded
    assert "--ignore-timing" in (flow.run_path / "yosys_synth.ys").read_text()
