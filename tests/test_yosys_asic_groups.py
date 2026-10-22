"""The ASIC settings of `yosys` are in groups, one per command or pass of the synthesis.

`hierarchy`, `coarse`, `arithmetic`, `memory`, `sequential`, `abc` and `finish` hold the settings
that only a mapping to a liberty library acts on. The platform gives the default of a leaf that
the user leaves unset, and one pure method of the flow (`Yosys.asic_values`) resolves them, so no
leaf is assigned after validation. These tests use the `tiny` platform (`tests/tiny_platform.py`)
and render the script of the liberty mapping in both script formats.
"""

import contextlib
import json
import re
import shutil
from pathlib import Path
from typing import Any, List, get_args

import pytest

from xeda.dataclass import input_names, tier_of
from xeda.flow import FlowException, FlowSettingsError, flowrun_hash
from xeda.flow.settings_group import SettingsGroup
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.settings_layers import compose_flow_settings
from xeda.flows import Yosys, YosysFpga
from xeda.flows.yosys import asic_groups
from xeda.introspect import settings_info
from xeda.units import Time

from .test_yosys_asic import (
    SCRIPT_FORMATS,
    _commands,
    _design,
    _scripted_launch,
    _tiny,
    _words,
)
from .tiny_platform import TINY, tiny_settings, tiny_store
from .tool_utils import require_yosys, use_fake_asic_tools
from .yosys_format import force_script_format

GROUPS = ("hierarchy", "coarse", "arithmetic", "memory", "sequential", "abc", "finish")

#: Every leaf of the groups with its tier.
LEAF_TIERS = {
    "hierarchy.keep_min_cost": "common",
    "hierarchy.keep_instances": "advanced",
    "hierarchy.separator": "advanced",
    "hierarchy.cross_boundary_opt": "advanced",
    "coarse.fsm": "advanced",
    "coarse.share": "advanced",
    "coarse.latches": "advanced",
    "coarse.opt_dff_sat": "advanced",
    "arithmetic.adder_architecture": "common",
    "arithmetic.adder_map": "common",
    "arithmetic.adder_map_file": "advanced",
    "arithmetic.wrap_operators": "advanced",
    "arithmetic.wrapped_adders": "advanced",
    "arithmetic.wrapped_multipliers": "advanced",
    "arithmetic.alumacc": "expert",
    "arithmetic.multiplier": "advanced",
    "arithmetic.arith_tree": "expert",
    "arithmetic.extract_fa.full": "advanced",
    "arithmetic.extract_fa.half": "advanced",
    "arithmetic.extract_fa.max_depth": "advanced",
    "arithmetic.extract_fa.max_breadth": "advanced",
    "memory.libmap": "advanced",
    "memory.techmap": "advanced",
    "memory.max_bits": "advanced",
    "memory.mock_large": "advanced",
    "memory.keep_mocked": "advanced",
    "memory.merge_ff": "expert",
    "memory.rw_check": "expert",
    "sequential.latch_map": "advanced",
    "sequential.latch_map_file": "advanced",
    "sequential.clockgate_map": "advanced",
    "sequential.clockgate_map_file": "advanced",
    "sequential.dff_liberty": "advanced",
    "sequential.dff_map": "advanced",
    "sequential.clock_gating.enabled": "advanced",
    "sequential.clock_gating.pos_icg": "advanced",
    "sequential.clock_gating.neg_icg": "advanced",
    "sequential.clock_gating.min_width": "advanced",
    "sequential.clock_gating.tie_lo": "expert",
    "sequential.retime_modules": "advanced",
    "sequential.tristate": "advanced",
    "sequential.min_enable_fanout": "expert",
    "sequential.min_sync_reset_fanout": "expert",
    "abc.strategy": "common",
    "abc.mapper": "advanced",
    "abc.driver_cell": "common",
    "abc.load": "common",
    "abc.delay_target": "advanced",
    "abc.clock_derating": "expert",
    "abc.buffering": "advanced",
    "abc.sizing": "advanced",
    "abc.max_fanout": "advanced",
    "abc.max_slew": "advanced",
    "abc.mfs3": "expert",
    "abc.area_mapper": "expert",
    "abc.legacy_refactor": "expert",
    "abc.legacy_rewrite": "expert",
    "abc.keep_ff": "expert",
    "abc.liberty_args": "expert",
    "abc.dress": "expert",
    "finish.tie_undefined": "common",
    "finish.tie_cells": "common",
    "finish.buffer_cell": "common",
    "finish.insert_tie_cells": "advanced",
    "finish.insert_buffers": "advanced",
    "finish.drop_formal": "advanced",
    "finish.names": "advanced",
    "finish.normalize_single_bit": "expert",
    "finish.check_pre": "expert",
    "finish.allow_tristate": "expert",
}

#: The scripts of the strategies, as `abc -script` takes them: commands separated by `;` and the
#: words of a command by `,`.
AREA = "+strash;dch;map,-B,0.9;topo;stime,-c;buffer,-c;upsize,-c;dnsize,-c"
SPEED_TAIL = "buffer,-c;topo;stime,-c;upsize,-c;dnsize,-c"
SPEED = "+&get,-n;&st;&dch;&nf;" + "&st;&syn2;&if,-g,-K,6;&synch2;&nf;" * 5 + "&put;" + SPEED_TAIL
SPEED_ROUNDTRIP = (
    "+&get,-n;&st;&dch;&nf;&put;"
    + "&get,-n;&st;&syn2;&if,-g,-K,6;&synch2;&nf;&put;" * 5
    + SPEED_TAIL
)


def _abc_script(commands: list[list[str]]) -> str | None:
    """The `-script` word of the `abc` command, or None."""
    (abc,) = _words(commands, "abc")
    return abc[abc.index("-script") + 1] if "-script" in abc else None


def _run(tmp_path, monkeypatch, script_format: str, **given: Any) -> list[list[str]]:
    """The commands of the script of `script_format` that `yosys` renders for the `tiny` platform
    and `given`."""
    force_script_format(monkeypatch, script_format)
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, _tiny(tmp_path, **given))
    return _commands(run_dir, script_format, tmp_path)


def _pdk(tmp_path: Path) -> Path:
    return tmp_path / "store" / "tiny"


# ------------------------------------------------------------------------------ the model


def test_yosys_has_the_seven_groups_in_order_and_a_flat_list_of_techmap_files():
    fields = list(Yosys.Settings.model_fields)
    assert [name for name in fields if name in GROUPS] == list(GROUPS)
    positions = [fields.index(name) for name in GROUPS]
    assert positions == sorted(positions)
    for name in GROUPS:
        assert issubclass(Yosys.Settings.model_fields[name].annotation, SettingsGroup)
        assert Yosys.Settings.model_fields[name].description
    assert Yosys.Settings().techmap_files == []
    assert Yosys.Settings.model_fields["techmap_files"].annotation == List[asic_groups.TechmapFile]


def test_the_defaults_of_the_first_leaves():
    settings = Yosys.Settings()
    assert settings.hierarchy.keep_min_cost is None
    assert settings.arithmetic.adder_architecture == "yosys"
    assert settings.arithmetic.adder_map == "platform"
    assert settings.arithmetic.adder_map_file is None
    assert settings.arithmetic.wrap_operators is False
    assert settings.sequential.latch_map == "platform"
    assert settings.sequential.latch_map_file is None
    assert settings.sequential.clockgate_map == "platform"
    assert settings.sequential.clockgate_map_file is None
    assert settings.sequential.dff_liberty is None
    assert settings.abc.strategy == "area"
    assert settings.abc.mapper == "abc"
    assert settings.abc.driver_cell is None and settings.abc.load is None
    assert settings.abc.delay_target is None
    assert settings.finish.tie_undefined == "zero"
    assert settings.finish.tie_cells is None and settings.finish.buffer_cell is None


def test_the_enum_leaves_take_exactly_the_documented_values():
    fields = Yosys.Settings
    assert set(get_args(asic_groups.AbcGroup.model_fields["strategy"].annotation)) == {
        "area",
        "speed",
        "speed_roundtrip",
        "yosys",
        *asic_groups.COMPOSED_STRATEGIES,
    }
    assert set(get_args(asic_groups.AbcGroup.model_fields["mapper"].annotation)) == {
        "abc",
        "abc_new",
    }
    assert set(get_args(asic_groups.FinishGroup.model_fields["tie_undefined"].annotation)) == {
        "zero",
        "one",
        "none",
    }
    assert set(
        get_args(asic_groups.ArithmeticGroup.model_fields["adder_architecture"].annotation)
    ) == {"yosys", "kogge_stone", "han_carlson", "sklansky"}
    assert set(get_args(asic_groups.ArithmeticGroup.model_fields["adder_map"].annotation)) == {
        "platform",
        "none",
    }
    assert fields is Yosys.Settings


@pytest.mark.parametrize(
    "key,value",
    [
        ("hierarchy.keep_min_cost", 0),
        ("abc.load", "-1 fF"),
        ("abc.delay_target", "-5 ps"),
        ("abc.strategy", "fast"),
        ("abc.mapper", "abc9"),
        ("finish.tie_undefined", "x"),
        ("arithmetic.adder_architecture", "kogge-stone"),
        ("finish.buffer_cell", ["BUF_X1", "A"]),
    ],
)
def test_a_leaf_refuses_a_value_outside_its_type(key, value):
    group, leaf = key.split(".")
    with pytest.raises(FlowSettingsError):
        Yosys.Settings.from_input({group: {leaf: value}})


def test_a_buffer_cell_is_a_cell_and_its_two_ports():
    settings = Yosys.Settings.from_input({"finish": {"buffer_cell": ["BUF_X1", "A", "Z"]}})
    assert tuple(settings.finish.buffer_cell) == ("BUF_X1", "A", "Z")


def test_the_delay_target_is_auto_a_time_or_unset():
    for value in ("auto", None):
        assert Yosys.Settings.from_input({"abc": {"delay_target": value}}).abc.delay_target == value
    for text in ("1500 ps", "1.5 ns", "1500.5 ps"):
        target = Yosys.Settings.from_input({"abc": {"delay_target": text}}).abc.delay_target
        assert target == Time(text)


def test_the_defaults_survive_a_round_trip_through_settings_json():
    settings = Yosys.Settings.from_input(
        {
            "abc": {"strategy": "speed", "delay_target": "auto", "load": "2.5 fF"},
            "finish": {
                "tie_cells": {"hi": ["H", "Z"], "lo": ["L", "Z"]},
                "buffer_cell": ["B", "A", "Z"],
            },
            "sequential": {"latch_map_file": "x.v"},
            "techmap_files": "a.v,b.v",
        }
    )
    again = Yosys.Settings.from_input(json.loads(json.dumps(settings.model_dump(mode="json"))))
    assert again == settings
    default = Yosys.Settings()
    reloaded = Yosys.Settings.from_input(default.model_dump(mode="json"))
    assert reloaded.model_dump() == default.model_dump()


def test_list_settings_lists_every_leaf_with_its_tier():
    fields = {field["name"]: field for field in settings_info("yosys")["fields"]}
    for key, tier in LEAF_TIERS.items():
        assert key in fields, key
        assert fields[key]["tier"] == tier, key
        assert fields[key]["description"], key
        assert fields[key]["group"] == key.split(".")[0], key
    assert "techmap_files" in fields and fields["techmap_files"]["group"] is None
    for name in ("abc.strategy", "arithmetic.adder_architecture"):
        assert fields[name]["enum"], name


def test_the_cli_lists_the_leaves_as_json():
    from click.testing import CliRunner

    from xeda.cli import cli

    result = CliRunner().invoke(cli, ["list-settings", "yosys", "--json"])
    assert result.exit_code == 0, result.output
    names = {field["name"] for field in json.loads(result.stdout)["fields"]}
    assert set(LEAF_TIERS) <= names


def _leaves(group: type[SettingsGroup], key: str) -> dict[str, Any]:
    """The leaves of `group` by dotted key, with their field. A sub-group is no leaf: its own
    leaves are."""
    found: dict[str, Any] = {}
    for name, info in group.model_fields.items():
        inner = info.annotation
        if isinstance(inner, type) and issubclass(inner, SettingsGroup):
            assert info.description, f"{key}.{name}"
            found.update(_leaves(inner, f"{key}.{name}"))
        else:
            assert tier_of(group, name) is not None, f"{key}.{name}"
            found[f"{key}.{name}"] = info
    return found


def test_every_leaf_has_a_tier_a_description_and_no_leaf_is_missing_from_the_table():
    leaves: set[str] = set()
    for name in GROUPS:
        group = Yosys.Settings.model_fields[name].annotation
        for key, info in _leaves(group, name).items():
            leaves.add(key)
            assert info.description, key
    assert leaves == set(LEAF_TIERS)


def test_the_tier_table_holds_the_tier_of_each_leaf():
    for key, expected in LEAF_TIERS.items():
        *path, leaf = key.split(".")
        group = Yosys.Settings.model_fields[path[0]].annotation
        for name in path[1:]:
            group = group.model_fields[name].annotation
        assert tier_of(group, leaf) == expected, key


# ------------------------------------------------------------------------------ the removed names

#: The removed flat settings of `yosys`, a value for each, and what the message says to use.
REMOVED = [
    (
        "optimize",
        "speed",
        "`abc.strategy` (`speed_roundtrip` for `speed`, and `yosys` for `null`)",
    ),
    ("abc_driver_cell", "BUF_X1", "`abc.driver_cell`"),
    ("abc_load_in_ff", 1.5, "`abc.load`"),
    (
        "adder_map",
        "adders.v",
        "`arithmetic.adder_map_file` for a file, or `arithmetic.adder_map: none`",
    ),
    (
        "clockgate_map",
        "gate.v",
        "`sequential.clockgate_map_file` for a file, or `sequential.clockgate_map: none`",
    ),
    ("dff_liberty", "dff.lib", "`sequential.dff_liberty`"),
    ("dff_library", "dff.lib", "`sequential.dff_liberty`"),
    (
        "other_maps",
        ["latch.v"],
        "`sequential.latch_map_file` for the latch map, `sequential.latch_map: none` for no latch "
        "map, and `techmap_files` for other maps",
    ),
    ("hilomap", {"hi": ["H", "Z"], "lo": ["L", "Z"]}, "`finish.tie_cells`"),
    ("insbuf", ["BUF", "A", "Z"], "`finish.buffer_cell`"),
]


@pytest.mark.parametrize("name,value,replacement", REMOVED)
def test_a_removed_setting_names_what_replaced_it(name, value, replacement):
    with pytest.raises(FlowSettingsError) as refused:
        Yosys.Settings.from_input({name: value})
    assert f"`{name}` was removed: use {replacement}" in str(refused.value)


@pytest.mark.parametrize("name,value,replacement", REMOVED)
def test_a_removed_setting_given_with_s_names_what_replaced_it(name, value, replacement):
    given = "x" if not isinstance(value, (int, float)) else str(value)
    with pytest.raises(FlowSettingsError) as refused:
        Yosys.Settings.from_input(compose_flow_settings(Yosys, [], [f"{name}={given}"]))
    assert f"`{name}` was removed: use {replacement}" in str(refused.value)


@pytest.mark.parametrize("name", [name for name, _, _ in REMOVED])
def test_a_removed_name_is_neither_a_field_nor_an_alias(name):
    assert name not in Yosys.Settings.model_fields
    assert name not in input_names(Yosys.Settings)
    assert name not in {
        alias for info in Yosys.Settings.model_fields.values() for alias in (info.alias,) if alias
    }


def test_yosys_fpga_keeps_its_clockgate_map():
    assert "clockgate_map" in YosysFpga.Settings.model_fields
    assert "clockgate_map" not in Yosys.Settings.model_fields


# ------------------------------------------------------------------------------ what is rendered


@SCRIPT_FORMATS
def test_the_default_settings_render_what_the_platform_gives(tmp_path, monkeypatch, script_format):
    commands = _run(tmp_path, monkeypatch, script_format)
    pdk = _pdk(tmp_path)
    assert _words(commands, "extract_fa") == [["extract_fa"]]
    assert ["techmap", "-map", str(pdk / "cells_adders.v")] in commands
    assert ["techmap", "-map", str(pdk / "cells_latch.v")] in commands
    assert ["read_verilog", "-defer", str(pdk / "cells_clkgate.v")] in commands
    assert _abc_script(commands) == AREA
    assert ["setundef", "-zero"] in commands
    assert _words(commands, "hilomap") == [
        ["hilomap", "-singleton", "-hicell", "TIEHI_SMALL", "Z", "-locell", "TIELO_X1", "Z"]
    ]
    assert _words(commands, "insbuf") == [["insbuf", "-buf", "BUF_X1", "A", "Z"]]
    for synth in _words(commands, "synth"):
        assert "-extra-map" not in synth


@SCRIPT_FORMATS
@pytest.mark.parametrize(
    "architecture,file",
    [
        ("kogge_stone", "kogge-stone"),
        ("han_carlson", "han-carlson"),
        ("sklansky", "sklansky"),
        ("yosys", None),
    ],
)
def test_adder_architecture_adds_the_extra_map_to_both_synth_commands(
    tmp_path, monkeypatch, script_format, architecture, file
):
    commands = _run(
        tmp_path, monkeypatch, script_format, arithmetic={"adder_architecture": architecture}
    )
    synths = _words(commands, "synth")
    assert len(synths) == 2
    for synth in synths:
        if file is None:
            assert "-extra-map" not in synth
        else:
            assert synth[synth.index("-extra-map") + 1] == f"+/choices/{file}.v"
    assert synths[0][:4] == ["synth", "-flatten", "-run", ":fine"]
    assert "-noabc" in synths[1]


@SCRIPT_FORMATS
@pytest.mark.parametrize("adder_map", ["platform", "none"])
def test_adder_map_none_leaves_out_the_full_adder_map(
    tmp_path, monkeypatch, script_format, adder_map
):
    commands = _run(tmp_path, monkeypatch, script_format, arithmetic={"adder_map": adder_map})
    adders = str(_pdk(tmp_path) / "cells_adders.v")
    assert (["extract_fa"] in commands) is (adder_map == "platform")
    assert (["techmap", "-map", adders] in commands) is (adder_map == "platform")
    assert ["techmap"] in commands, "the generic techmap stays"


@SCRIPT_FORMATS
def test_adder_map_file_replaces_the_adder_map_of_the_platform(
    tmp_path, monkeypatch, script_format
):
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "my_adders.v").write_text("module _unused_fa(); endmodule\n")
    commands = _run(
        tmp_path, monkeypatch, script_format, arithmetic={"adder_map_file": "my_adders.v"}
    )
    assert ["techmap", "-map", str(tmp_path / "design" / "my_adders.v")] in commands
    assert ["techmap", "-map", str(_pdk(tmp_path) / "cells_adders.v")] not in commands
    assert _words(commands, "extract_fa") == [["extract_fa"]]


@SCRIPT_FORMATS
@pytest.mark.parametrize(
    "strategy,script",
    [("area", AREA), ("speed", SPEED), ("speed_roundtrip", SPEED_ROUNDTRIP), ("yosys", None)],
)
def test_strategy_selects_the_abc_script(tmp_path, monkeypatch, script_format, strategy, script):
    commands = _run(tmp_path, monkeypatch, script_format, abc={"strategy": strategy})
    assert _abc_script(commands) == script


def test_the_speed_script_is_the_one_of_the_reference_flow_without_the_round_trips():
    """`speed` has no `&put`, `&get -n` pair between its rounds, and `speed_roundtrip` has them."""
    assert SPEED_ROUNDTRIP.count("&get,-n") == 6 and SPEED.count("&get,-n") == 1
    assert SPEED_ROUNDTRIP.replace("&put;&get,-n;", "") == SPEED
    assert AREA != SPEED != SPEED_ROUNDTRIP


@SCRIPT_FORMATS
@pytest.mark.parametrize(
    "value,line", [("zero", ["setundef", "-zero"]), ("one", ["setundef", "-one"])]
)
def test_tie_undefined_selects_the_setundef_flag(tmp_path, monkeypatch, script_format, value, line):
    commands = _run(tmp_path, monkeypatch, script_format, finish={"tie_undefined": value})
    assert _words(commands, "setundef") == [line]


@SCRIPT_FORMATS
def test_tie_undefined_none_leaves_out_the_setundef_line(tmp_path, monkeypatch, script_format):
    commands = _run(tmp_path, monkeypatch, script_format, finish={"tie_undefined": "none"})
    assert _words(commands, "setundef") == []
    names = [command[0] for command in commands]
    assert names.index("dfflibmap") < names.index("abc"), "the other commands stay in place"


@SCRIPT_FORMATS
@pytest.mark.parametrize(
    "target,expected",
    [("auto", "2000.000"), ("1500 ps", "1500.000"), ("1234.5 ps", "1234.500"), (None, None)],
    ids=["auto", "time", "fraction", "none"],
)
def test_delay_target_is_the_d_flag_of_abc(tmp_path, monkeypatch, script_format, target, expected):
    commands = _run(tmp_path, monkeypatch, script_format, abc={"delay_target": target})
    (abc,) = _words(commands, "abc")
    if expected is None:
        assert "-D" not in abc
    else:
        assert abc[abc.index("-D") + 1] == expected
        assert abc.count("-D") == 1


# ------------------------------------------------ a user value wins, and None takes the platform's


@SCRIPT_FORMATS
def test_a_user_latch_map_wins_over_the_platform_s(tmp_path, monkeypatch, script_format):
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "my_latch.v").write_text("module _unused(); endmodule\n")
    commands = _run(
        tmp_path, monkeypatch, script_format, sequential={"latch_map_file": "my_latch.v"}
    )
    assert ["techmap", "-map", str(tmp_path / "design" / "my_latch.v")] in commands
    assert ["techmap", "-map", str(_pdk(tmp_path) / "cells_latch.v")] not in commands
    assert len(_words(commands, "dfflegalize")) == 1


@SCRIPT_FORMATS
def test_a_user_clockgate_map_wins_over_the_platform_s(tmp_path, monkeypatch, script_format):
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "my_gate.v").write_text("module cg(); endmodule\n")
    commands = _run(
        tmp_path, monkeypatch, script_format, sequential={"clockgate_map_file": "my_gate.v"}
    )
    assert ["read_verilog", "-defer", str(tmp_path / "design" / "my_gate.v")] in commands
    assert ["read_verilog", "-defer", str(_pdk(tmp_path) / "cells_clkgate.v")] not in commands


@SCRIPT_FORMATS
def test_a_dff_liberty_replaces_the_liberty_files_on_dfflibmap_only(
    tmp_path, monkeypatch, script_format
):
    (tmp_path / "design").mkdir()
    shutil.copy(TINY / "tiny.lib", tmp_path / "design" / "dff.lib")
    commands = _run(tmp_path, monkeypatch, script_format, sequential={"dff_liberty": "dff.lib"})
    dff = str(tmp_path / "design" / "dff.lib")
    (dfflibmap,) = _words(commands, "dfflibmap")
    assert dfflibmap[:3] == ["dfflibmap", "-liberty", dff]
    (abc,) = _words(commands, "abc")
    assert str(_pdk(tmp_path) / "tiny.lib") in abc and dff not in abc
    reads = [c for c in commands if c[0] == "read_liberty" and dff in c]
    assert len(reads) == 1


@SCRIPT_FORMATS
def test_user_values_of_the_cell_leaves_win_over_the_platform_s(
    tmp_path, monkeypatch, script_format
):
    given = {
        "abc": {"driver_cell": "INV_X1", "load": "2.5 fF"},
        "finish": {
            "tie_cells": {"hi": ["TIEHI_BIG", "Z"], "lo": ["TIELO_X1", "Y"], "singleton": False},
            "buffer_cell": ["INV_X1", "A", "ZN"],
        },
    }
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, _tiny(tmp_path, **given))
    commands = _commands(run_dir, script_format, tmp_path)
    assert (run_dir / "abc.constr").read_text() == "set_driving_cell INV_X1\nset_load 2.5\n"
    assert _words(commands, "hilomap") == [
        ["hilomap", "-hicell", "TIEHI_BIG", "Z", "-locell", "TIELO_X1", "Y"]
    ]
    assert _words(commands, "insbuf") == [["insbuf", "-buf", "INV_X1", "A", "ZN"]]


@SCRIPT_FORMATS
def test_a_leaf_left_unset_takes_the_value_of_the_platform(tmp_path, monkeypatch, script_format):
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, _tiny(tmp_path))
    assert (run_dir / "abc.constr").read_text() == "set_driving_cell BUF_X1\nset_load 3.5\n"
    settings = Yosys.Settings.from_input(
        json.loads((run_dir / "settings.json").read_text())["flow_settings"]
    )
    assert settings.abc.driver_cell is None and settings.abc.load is None


def test_a_leaf_that_the_user_gave_with_a_partial_group_keeps_the_other_defaults(
    tmp_path, monkeypatch
):
    run_dir = _scripted_launch(tmp_path, monkeypatch, Yosys, _tiny(tmp_path, abc={"load": "1 fF"}))
    assert (run_dir / "abc.constr").read_text() == "set_driving_cell BUF_X1\nset_load 1.0\n"


def test_no_group_leaf_is_assigned_after_validation(tmp_path, monkeypatch):
    """The settings the flow ends with hold the groups as the user gave them."""
    run_dir = _scripted_launch(
        tmp_path, monkeypatch, Yosys, _tiny(tmp_path, abc={"strategy": "speed"})
    )
    effective = json.loads((run_dir / "settings.json").read_text())["effective_flow_settings"]
    flow_settings = json.loads((run_dir / "settings.json").read_text())["flow_settings"]
    default = Yosys.Settings().model_dump(mode="json")
    for group in GROUPS:
        expected = dict(default[group])
        if group == "abc":
            expected["strategy"] = "speed"
        assert effective[group] == expected, group
        assert flow_settings.get(group, expected) == expected or group == "abc"
    assert effective["abc_script"] is None
    assert effective["techmap_files"] == []


def test_the_pure_method_resolves_the_values_and_changes_no_setting(tmp_path):
    store = tiny_store(tmp_path)
    design = _design(tmp_path / "design")
    settings = Yosys.Settings.from_input(
        tiny_settings(
            store,
            clock={"period": "2 ns"},
            abc={"load": "1.5 fF", "delay_target": "auto"},
            techmap_files=["extra.v"],
            memory={"libmap": ["mem.txt"], "techmap": "mem_map.v"},
        ),
        design_root=design.design_root,
    )
    before = settings.model_dump()
    flow = Yosys(settings, design, tmp_path / "run")
    (tmp_path / "run").mkdir()
    flow.init()
    flow.prepare_inputs()
    after_init = flow.settings.model_dump()
    values = flow.asic_values()
    assert flow.settings.model_dump() == after_init
    assert values == flow.asic_values()
    pdk = store / "tiny"
    assert values.latch_map == pdk / "cells_latch.v"
    assert values.clockgate_map == pdk / "cells_clkgate.v"
    assert values.adder_map == pdk / "cells_adders.v"
    assert values.techmap_files == (("pre_dff", design.design_root / "extra.v"),)
    assert values.driver_cell == "BUF_X1" and values.load == 1.5
    assert values.tie_cells is not None and values.tie_cells.hi == ("TIEHI_SMALL", "Z")
    assert values.buffer_cell == ("BUF_X1", "A", "Z")
    assert values.abc_script == AREA
    assert values.delay_target_ps == pytest.approx(2000.0)
    assert values.constraints == ("set_driving_cell BUF_X1", "set_load 1.5")
    assert before["abc"] == after_init["abc"]


def test_without_a_liberty_mapping_the_pure_method_gives_nothing(tmp_path):
    design = _design(tmp_path / "design")
    flow = Yosys(Yosys.Settings(gates=["AND"], clock={"period": "2 ns"}), design, tmp_path)
    values = flow.asic_values()
    assert values.latch_map is None and values.adder_map is None and values.abc_script is None
    assert values.constraints == () and values.techmap_files == ()


# ------------------------------------------------------------------------------ the refusals


def _refused(**given: Any) -> list[tuple[str, str]]:
    """Plan `given` and return what `check_settings_supported` refuses, as (key, message)."""
    settings = Yosys.Settings.from_input(given)
    with pytest.raises(FlowSettingsError) as refused:
        Yosys.check_settings_supported(settings)
    return [(key, message) for key, message, _, _ in refused.value.errors]


NO_LIBERTY = (
    "acts on mapping to a liberty library: give `platform`, `platform_file` or `liberty` too"
)

GENERIC = {
    "gates": "`gates` maps to generic cells instead",
    "lut": "`lut` maps to LUTs instead",
}


def no_liberty_message(key: str, mapping: dict) -> str:
    """What a group leaf `key` is told when `mapping` leaves no liberty mapping to act on."""
    for name, reason in GENERIC.items():
        if name in mapping:
            return f"`{key}` acts on mapping to a liberty library, and {reason}: delete `{key}` or `{name}`"
    return f"`{key}` {NO_LIBERTY}"


#: A leaf set to a non-default value, with a mapping that is no liberty mapping.
NO_MAPPING_CASES = [
    ("hierarchy.keep_min_cost", {"hierarchy": {"keep_min_cost": 5}}),
    ("arithmetic.adder_architecture", {"arithmetic": {"adder_architecture": "sklansky"}}),
    ("arithmetic.adder_map", {"arithmetic": {"adder_map": "none"}}),
    ("arithmetic.adder_map_file", {"arithmetic": {"adder_map_file": "a.v"}}),
    ("arithmetic.wrap_operators", {"arithmetic": {"wrap_operators": True}}),
    ("sequential.latch_map", {"sequential": {"latch_map": "none"}}),
    ("sequential.latch_map_file", {"sequential": {"latch_map_file": "l.v"}}),
    ("sequential.clockgate_map", {"sequential": {"clockgate_map": "none"}}),
    ("sequential.clockgate_map_file", {"sequential": {"clockgate_map_file": "c.v"}}),
    ("sequential.dff_liberty", {"sequential": {"dff_liberty": "d.lib"}}),
    ("abc.strategy", {"abc": {"strategy": "speed"}}),
    ("abc.mapper", {"abc": {"mapper": "abc_new"}}),
    ("abc.driver_cell", {"abc": {"driver_cell": "BUF_X1"}}),
    ("abc.load", {"abc": {"load": "1 fF"}}),
    ("abc.delay_target", {"abc": {"delay_target": "100 ps"}}),
    ("finish.tie_undefined", {"finish": {"tie_undefined": "one"}}),
    ("finish.tie_cells", {"finish": {"tie_cells": {"hi": ["H", "Z"], "lo": ["L", "Z"]}}}),
    ("finish.buffer_cell", {"finish": {"buffer_cell": ["B", "A", "Z"]}}),
    ("techmap_files", {"techmap_files": ["t.v"]}),
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
    ("memory.libmap", {"memory": {"libmap": ["m.txt"]}}),
    ("memory.techmap", {"memory": {"techmap": "m.v"}}),
    ("memory.max_bits", {"memory": {"max_bits": 8}}),
    ("memory.merge_ff", {"memory": {"merge_ff": False}}),
    ("memory.rw_check", {"memory": {"rw_check": False}}),
]


@pytest.mark.parametrize("mapping", [{"gates": ["AND"]}, {"lut": "4"}, {}], ids=str)
@pytest.mark.parametrize("key,given", NO_MAPPING_CASES, ids=[key for key, _ in NO_MAPPING_CASES])
def test_a_group_leaf_is_refused_when_no_liberty_mapping_runs(key, given, mapping):
    problems = dict(_refused(**given, **mapping))
    assert problems[key] == no_liberty_message(key, mapping)


def test_every_problem_comes_back_in_one_error():
    problems = _refused(
        abc={"strategy": "speed", "driver_cell": "X"},
        finish={"tie_undefined": "one"},
        techmap_files=["t.v"],
    )
    assert [key for key, _ in problems] == [
        "abc.strategy",
        "abc.driver_cell",
        "finish.tie_undefined",
        "techmap_files",
    ]


def test_a_default_leaf_is_not_refused_when_no_liberty_mapping_runs():
    Yosys.check_settings_supported(Yosys.Settings(gates=["AND"]))
    Yosys.check_settings_supported(Yosys.Settings(abc={"strategy": "area", "mapper": "abc"}))


def test_wrap_operators_false_and_the_default_mapper_are_not_refused():
    Yosys.check_settings_supported(
        Yosys.Settings(platform="nangate45", arithmetic={"wrap_operators": False})
    )


def test_abc_script_with_a_strategy_is_refused_naming_both():
    problems = dict(_refused(platform="nangate45", abc_script="+strash", abc={"strategy": "speed"}))
    assert "`abc_script`" in problems["abc_script"] and "`abc.strategy`" in problems["abc_script"]
    Yosys.check_settings_supported(Yosys.Settings(platform="nangate45", abc_script="+strash"))
    Yosys.check_settings_supported(
        Yosys.Settings(platform="nangate45", abc_script="+strash", abc={"strategy": "area"})
    )


def test_an_adder_map_file_with_adder_map_none_is_refused():
    problems = dict(
        _refused(platform="nangate45", arithmetic={"adder_map": "none", "adder_map_file": "a.v"})
    )
    message = problems["arithmetic.adder_map_file"]
    assert "`arithmetic.adder_map_file`" in message and "`arithmetic.adder_map: none`" in message


def test_the_auto_delay_target_needs_a_clock():
    problems = dict(_refused(platform="nangate45", abc={"delay_target": "auto"}))
    assert "`abc.delay_target: auto`" in problems["abc.delay_target"]
    Yosys.check_settings_supported(
        Yosys.Settings(platform="nangate45", abc={"delay_target": "auto"}, clock={"period": "2 ns"})
    )
    Yosys.check_settings_supported(
        Yosys.Settings(platform="nangate45", abc={"delay_target": "1 ns"})
    )


def test_the_refusals_arrive_at_planning_before_any_run_directory(tmp_path):
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with pytest.raises(FlowSettingsError, match="abc.strategy"):
        runner.plan(Yosys, _design(tmp_path / "design"), flow_settings=["abc.strategy=speed"])
    assert not (tmp_path / "xeda_run").exists()


# ------------------------------------------------------------------------------ input files


@pytest.fixture
def fake_yosys(tmp_path, monkeypatch) -> Path:
    use_fake_asic_tools(monkeypatch, tmp_path / "bin")
    return tmp_path


def _runner(tmp_path: Path) -> DefaultRunner:
    return DefaultRunner(tmp_path / "xeda_run", display_results=False)


def test_a_relative_group_path_resolves_against_the_design_root_and_not_the_working_directory(
    tmp_path,
):
    """The design root is not the working directory, and yosys runs in the run directory."""
    design = _design(tmp_path / "design")
    store = tiny_store(tmp_path)
    for name in ("latch.v", "cg.v", "fa.v", "extra.v", "mem.txt", "mem_map.v"):
        (design.design_root / name).write_text("module _unused(); endmodule\n")
    shutil.copy(TINY / "tiny.lib", design.design_root / "dff.lib")
    settings = Yosys.Settings.from_input(
        tiny_settings(
            store,
            sequential={
                "latch_map_file": "latch.v",
                "clockgate_map_file": "cg.v",
                "dff_liberty": "dff.lib",
            },
            arithmetic={"adder_map_file": "fa.v"},
            techmap_files=["extra.v"],
            memory={"libmap": ["mem.txt"], "techmap": "mem_map.v"},
        ),
        design_root=design.design_root,
    )
    run = tmp_path / "run"
    run.mkdir()
    flow = Yosys(settings, design, run)
    flow.init()
    values = flow.asic_values()
    root = design.design_root
    assert values.latch_map == root / "latch.v"
    assert values.clockgate_map == root / "cg.v"
    assert values.dff_liberty == root / "dff.lib"
    assert values.adder_map == root / "fa.v"
    assert values.techmap_files == (("pre_dff", root / "extra.v"),)
    assert values.memory_libmap == (root / "mem.txt",)
    assert values.memory_techmap == root / "mem_map.v"


def _given(store: Path, **extra: Any) -> dict[str, Any]:
    return tiny_settings(store, clock={"period": "2 ns"}, **extra)


def test_editing_a_user_latch_map_makes_the_next_launch_stale(fake_yosys):
    design = _design(fake_yosys / "design")
    latch = design.design_root / "my_latch.v"
    latch.write_text("module _unused(); endmodule\n")
    store = tiny_store(fake_yosys)
    given = _given(store, sequential={"latch_map_file": "my_latch.v"})
    runner = _runner(fake_yosys)
    first = runner.launch_flow(Yosys, design, given)
    assert first.succeeded
    _, states = _runner(fake_yosys).plan_with_freshness(Yosys, design, flow_settings=given)
    assert states["yosys"].state == "fresh"
    latch.write_text(latch.read_text() + "// edited\n")
    _, states = _runner(fake_yosys).plan_with_freshness(Yosys, design, flow_settings=given)
    assert states["yosys"].state == "stale"
    assert states["yosys"].reason == f"input changed: {latch.resolve()}"


def test_the_other_group_files_are_inputs_too(fake_yosys):
    design = _design(fake_yosys / "design")
    files = {
        "cg.v": {"sequential": {"clockgate_map_file": "cg.v"}},
        "dffs.v": {"sequential": {"dff_map": "dffs.v"}},
        "fa.v": {"arithmetic": {"adder_map_file": "fa.v"}},
        "extra.v": {"techmap_files": ["extra.v"]},
        "mem.txt": {"memory": {"libmap": ["mem.txt"]}},
        "mem_map.v": {"memory": {"techmap": "mem_map.v"}},
    }
    store = tiny_store(fake_yosys)
    for name, given in files.items():
        path = design.design_root / name
        path.write_text("module _unused(); endmodule\n")
        settings = _given(store, **given)
        assert _runner(fake_yosys).launch_flow(Yosys, design, settings).succeeded
        _, states = _runner(fake_yosys).plan_with_freshness(Yosys, design, flow_settings=settings)
        assert states["yosys"].state == "fresh", name
        path.write_text("module _unused2(); endmodule\n")
        _, states = _runner(fake_yosys).plan_with_freshness(Yosys, design, flow_settings=settings)
        assert states["yosys"].state == "stale", name


def test_moving_the_design_keeps_the_identity_of_the_run(tmp_path):
    store = tiny_store(tmp_path)
    hashes = []
    for place in ("one", "two"):
        root = tmp_path / place / "design"
        design = _design(root)
        (root / "my_latch.v").write_text("module _unused(); endmodule\n")
        settings = Yosys.Settings.from_input(
            _given(store, sequential={"latch_map_file": "my_latch.v"}), design_root=root
        )
        hashes.append(flowrun_hash("yosys", settings, design.name))
    assert hashes[0] == hashes[1]
    other = Yosys.Settings.from_input(
        _given(store, sequential={"latch_map_file": "other.v"}),
        design_root=tmp_path / "one" / "design",
    )
    assert flowrun_hash("yosys", other, "mac") != hashes[0]


# ------------------------------------------------------------------------------ the templates

TEMPLATES = Path(asic_groups.__file__).parent / "templates"
MOVED = (
    "adder_map|other_maps|dff_liberty|hilomap|insbuf|optimize|abc_driver_cell|abc_load_in_ff|"
    "clockgate_map"
)


def test_the_liberty_templates_read_none_of_the_moved_flat_settings():
    pattern = re.compile(rf"settings\.({MOVED})\b")
    for path in sorted(TEMPLATES.iterdir()):
        if path.suffix in (".ys", ".tcl"):
            assert not pattern.search(path.read_text()), path.name


def test_the_reader_templates_take_the_clockgate_map_from_a_render_variable():
    for suffix in ("ys", "tcl"):
        text = (TEMPLATES / f"read_files.{suffix}").read_text()
        assert "settings.clockgate_map" not in text
        assert "clockgate_map" in text


@pytest.mark.parametrize("script_format", ["ys"])  # `yosys_fpga` writes no Tcl script
def test_yosys_fpga_still_reads_its_own_clockgate_map(tmp_path, script_format):
    from .test_yosys_templates import _fpga_settings, _render

    script = _render(
        YosysFpga,
        _fpga_settings(clockgate_map=tmp_path / "cg.v"),
        tmp_path,
    )
    assert re.search(rf'read_verilog -defer "?{re.escape(str(tmp_path / "cg.v"))}"?$', script, re.M)
    plain = _render(YosysFpga, _fpga_settings(), tmp_path)
    assert not re.search(r"read_verilog -defer \S*cg\.v", plain)


# ------------------------------------------------------------------------------ real yosys


def test_real_yosys_runs_the_yosys_strategy_with_no_script(tmp_path):
    require_yosys()
    design = _design(tmp_path)
    with contextlib.suppress(FlowException):
        DefaultRunner(tmp_path / "xeda_run").run_flow(
            Yosys, design, _tiny(tmp_path, abc={"strategy": "yosys"})
        )
    (results,) = tmp_path.glob("xeda_run/mac/yosys/results.json")
    assert json.loads(results.read_text())["outcome"] == "passed"
    script = (results.parent / "yosys_synth.ys").read_text()
    assert "-script" not in script


@pytest.mark.parametrize("architecture", ["kogge_stone", "han_carlson", "sklansky"])
def test_real_yosys_takes_each_adder_architecture(tmp_path, architecture):
    require_yosys()
    design = _design(tmp_path)
    with contextlib.suppress(FlowException):
        DefaultRunner(tmp_path / "xeda_run").run_flow(
            Yosys,
            design,
            _tiny(tmp_path, arithmetic={"adder_architecture": architecture, "adder_map": "none"}),
        )
    (results,) = tmp_path.glob("xeda_run/mac/yosys/results.json")
    assert json.loads(results.read_text())["outcome"] == "passed"


def test_a_platform_with_gates_gives_both_messages_and_the_right_reason_for_the_leaf():
    problems = _refused(platform="nangate45", gates=["AND", "OR"], abc={"strategy": "speed"})
    messages = dict(problems)
    assert "`gates` and `platform` cannot both be set" in messages["gates"]
    assert messages["abc.strategy"] == no_liberty_message("abc.strategy", {"gates": 1})
    assert "give `platform`" not in messages["abc.strategy"]


def test_a_platform_with_lut_names_lut_as_the_reason():
    messages = dict(_refused(platform="nangate45", lut="4", abc={"strategy": "speed"}))
    assert "`lut` maps to LUTs instead" in messages["abc.strategy"]
    assert "`lut` and `platform` cannot both be set" in messages["lut"]
