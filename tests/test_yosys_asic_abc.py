"""The ABC options of the `yosys` ASIC synthesis: the composed strategies and their knobs, the
clock derating, and the flags of the `abc` command.

`area_0` to `area_2` and `delay_0` to `delay_3` are the strategies that LibreLane composes from
the steps of ABC (`construct_abc_script.py`, Apache-2.0, see `data/asic/NOTICE.md`). The tests
hold the script that LibreLane writes for each of them, with and without options, and check that
the flow hands it to `abc -script` in both script formats. The tests at the end run a real yosys
on the `tiny` platform for each strategy.
"""

import contextlib
import json
from typing import Any, get_args

import pytest

from xeda.dataclass import tier_of
from xeda.flow import FlowException
from xeda.flow_runner import DefaultRunner
from xeda.flows import Yosys
from xeda.flows.yosys import abc_strategy_script, asic_groups
from xeda.flows.yosys.abc_strategies import Composition
from xeda.units import Time

from .test_yosys_asic import SCRIPT_FORMATS, _design, _tiny, _words
from .test_yosys_asic_groups import no_liberty_message, _abc_script, _refused, _run
from .test_yosys_asic_sequential import _enable_design
from .tool_utils import require_yosys, require_yosys_tcl
from .yosys_format import forced_script_format

#: The script of each composed strategy at its defaults, as `abc -script` takes it. The text is
#: the script that `construct_abc_script.py` of LibreLane writes for `AREA 0` to `DELAY 3` (commands
#: separated by `;`, words by `,`, and `stime -p; print_stats -m` at the end).
COMPOSED = {
    "area_0": (
        "+fx;mfs;strash;drf,-l;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;"
        "drf,-l,-z;drw,-l,-z;balance;retime,-M,5;scleanup;fraig_store;balance;fraig_store;"
        "balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;"
        "fraig_store;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;"
        "drw,-l,-z;balance;fraig_store;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;"
        "balance;drf,-l,-z;drw,-l,-z;balance;fraig_store;fraig_restore;"
        "amap,-m,-Q,0.1,-F,20,-A,20,-C,5000;retime;&get,-n;&st;&dch;&nf;&put;stime,-p;"
        "print_stats,-m"
    ),
    "area_1": (
        "+fx;mfs;strash;drf,-l;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;"
        "drf,-l,-z;drw,-l,-z;balance;retime,-M,5;scleanup;fraig_store;balance;fraig_store;"
        "balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;"
        "fraig_store;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;"
        "drw,-l,-z;balance;fraig_store;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;"
        "balance;drf,-l,-z;drw,-l,-z;balance;fraig_store;fraig_restore;"
        "amap,-m,-Q,0.1,-F,20,-A,20,-C,5000;fraig_store;balance;fraig_store;balance;drw,-l;"
        "drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;fraig_store;"
        "balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;"
        "fraig_store;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;"
        "drw,-l,-z;balance;fraig_store;fraig_restore;amap,-m,-Q,0.1,-F,20,-A,20,-C,5000;"
        "retime;&get,-n;&st;&dch;&nf;&put;stime,-p;print_stats,-m"
    ),
    "area_2": (
        "+fx;mfs;strash;drf,-l;fraig_store;balance;fraig_store;balance;drw,-l;drf,-l;balance;"
        "drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;fraig_store;balance;drw,-l;"
        "drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;fraig_store;"
        "balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;"
        "fraig_store;fraig_restore;retime,-M,5;scleanup;fraig_store;balance;fraig_store;"
        "balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;"
        "fraig_store;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;"
        "drw,-l,-z;balance;fraig_store;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;"
        "balance;drf,-l,-z;drw,-l,-z;balance;fraig_store;fraig_restore;"
        "amap,-m,-Q,0.1,-F,20,-A,20,-C,5000;fraig_store;balance;fraig_store;balance;drw,-l;"
        "drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;fraig_store;"
        "balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;"
        "fraig_store;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;"
        "drw,-l,-z;balance;fraig_store;fraig_restore;amap,-m,-Q,0.1,-F,20,-A,20,-C,5000;"
        "retime;&get,-n;&st;&dch;&nf;&put;stime,-p;print_stats,-m"
    ),
    "delay_0": (
        "+fx;mfs;strash;drf,-l;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;"
        "drf,-l,-z;drw,-l,-z;balance;retime,-M,6;scleanup;map,-p,-B,0.2,-A,0.9,-M,0;retime;"
        "&get,-n;&st;&dch;&nf;&put;stime,-p;print_stats,-m"
    ),
    "delay_1": (
        "+fx;mfs;strash;drf,-l;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;"
        "drf,-l,-z;drw,-l,-z;balance;retime,-M,6;scleanup;fraig_store;balance;fraig_store;"
        "balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;"
        "fraig_store;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;"
        "drw,-l,-z;balance;fraig_store;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;"
        "balance;drf,-l,-z;drw,-l,-z;balance;fraig_store;fraig_restore;"
        "map,-p,-B,0.2,-A,0.9,-M,0;fraig_store;balance;fraig_store;balance;drw,-l;drf,-l;"
        "balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;fraig_store;balance;"
        "drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;"
        "fraig_store;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;"
        "drw,-l,-z;balance;fraig_store;fraig_restore;map;retime;&get,-n;&st;&dch;&nf;&put;"
        "stime,-p;print_stats,-m"
    ),
    "delay_2": (
        "+fx;mfs;strash;drf,-l;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;"
        "drf,-l,-z;drw,-l,-z;balance;retime,-M,6;scleanup;fraig_store;balance;drw,-l;drf,-l;"
        "balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;fraig_store;balance;"
        "drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;"
        "fraig_store;fraig_restore;map,-p,-B,0.2,-A,0.9,-M,0;fraig_store;balance;drw,-l;"
        "drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;fraig_store;"
        "balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;"
        "fraig_store;fraig_restore;map;retime;&get,-n;&st;&dch;&nf;&put;stime,-p;"
        "print_stats,-m"
    ),
    "delay_3": (
        "+fx;mfs;strash;drf,-l;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;"
        "drf,-l,-z;drw,-l,-z;balance;retime,-M,5;scleanup;fraig_store;balance;fraig_store;"
        "balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;"
        "fraig_store;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;"
        "drw,-l,-z;balance;fraig_store;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;"
        "balance;drf,-l,-z;drw,-l,-z;balance;fraig_store;fraig_restore;"
        "amap,-m,-Q,0.1,-F,20,-A,20,-C,5000;fraig_store;balance;fraig_store;balance;drw,-l;"
        "drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;fraig_store;"
        "balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;"
        "fraig_store;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;"
        "drw,-l,-z;balance;fraig_store;fraig_restore;map,-p,-B,0.2,-A,0.9,-M,0;retime;"
        "&get,-n;&st;&dch;&nf;&put;stime,-p;print_stats,-m"
    ),
}

#: Strategies with knobs, each with the script that LibreLane writes for the same options.
COMPOSED_WITH_KNOBS = [
    (
        "area_0",
        {"buffering": True, "max_fanout": 8, "max_slew": 150},
        (
            "+fx;mfs;strash;drf,-l;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;"
            "drf,-l,-z;drw,-l,-z;balance;retime,-M,5;scleanup;fraig_store;balance;fraig_store;"
            "balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;"
            "fraig_store;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;"
            "drw,-l,-z;balance;fraig_store;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;"
            "balance;drf,-l,-z;drw,-l,-z;balance;fraig_store;fraig_restore;"
            "amap,-m,-Q,0.1,-F,20,-A,20,-C,5000;retime;&get,-n;&st;&dch;&nf;&put;"
            "buffer,-N,8,-S,150;upsize;dnsize;stime,-p;print_stats,-m"
        ),
    ),
    (
        "delay_2",
        {"sizing": True},
        (
            "+fx;mfs;strash;drf,-l;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;"
            "drf,-l,-z;drw,-l,-z;balance;retime,-M,6;scleanup;fraig_store;balance;drw,-l;drf,-l;"
            "balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;fraig_store;balance;"
            "drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;"
            "fraig_store;fraig_restore;map,-p,-B,0.2,-A,0.9,-M,0;fraig_store;balance;drw,-l;"
            "drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;fraig_store;"
            "balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;"
            "fraig_store;fraig_restore;map;retime;&get,-n;&st;&dch;&nf;&put;upsize;dnsize;"
            "stime,-p;print_stats,-m"
        ),
    ),
    (
        "area_1",
        {"mfs3": True, "area_mapper": "nf"},
        (
            "+fx;mfs;strash;drf,-l;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;"
            "drf,-l,-z;drw,-l,-z;balance;retime,-M,5;scleanup;fraig_store;balance;fraig_store;"
            "balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;"
            "fraig_store;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;"
            "drw,-l,-z;balance;fraig_store;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;"
            "balance;drf,-l,-z;drw,-l,-z;balance;fraig_store;fraig_restore;&get,-n;&nf,-R,1000;"
            "&put;fraig_store;balance;fraig_store;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;"
            "balance;drf,-l,-z;drw,-l,-z;balance;fraig_store;balance;drw,-l;drf,-l;balance;"
            "drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;fraig_store;balance;drw,-l;"
            "drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;fraig_store;"
            "fraig_restore;&get,-n;&nf,-R,1000;&put;mfs3,-aemvz,-I,4,-O,2;retime;&get,-n;&st;"
            "&dch;&nf;&put;stime,-p;print_stats,-m"
        ),
    ),
    (
        "delay_3",
        {"mfs3": True, "area_mapper": "nf", "buffering": True, "max_fanout": 4},
        (
            "+fx;mfs;strash;drf,-l;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;"
            "drf,-l,-z;drw,-l,-z;balance;retime,-M,5;scleanup;fraig_store;balance;fraig_store;"
            "balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;"
            "fraig_store;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;"
            "drw,-l,-z;balance;fraig_store;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;"
            "balance;drf,-l,-z;drw,-l,-z;balance;fraig_store;fraig_restore;&get,-n;&nf,-R,1000;"
            "&put;fraig_store;balance;fraig_store;balance;drw,-l;drf,-l;balance;drw,-l;drw,-l,-z;"
            "balance;drf,-l,-z;drw,-l,-z;balance;fraig_store;balance;drw,-l;drf,-l;balance;"
            "drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;fraig_store;balance;drw,-l;"
            "drf,-l;balance;drw,-l;drw,-l,-z;balance;drf,-l,-z;drw,-l,-z;balance;fraig_store;"
            "fraig_restore;map,-p,-B,0.2,-A,0.9,-M,0;mfs3,-emvz,-I,4,-O,2;retime;&get,-n;&st;"
            "&dch;&nf;&put;buffer,-N,4;upsize;dnsize;stime,-p;print_stats,-m"
        ),
    ),
    (
        "delay_0",
        {"mfs3": True, "legacy_refactor": True},
        (
            "+fx;mfs;strash;refactor;balance;drw,-l;refactor;balance;drw,-l;drw,-l,-z;balance;"
            "refactor,-z;drw,-l,-z;balance;retime,-M,6;scleanup;map,-p,-B,0.2,-A,0.9,-M,0;"
            "mfs3,-emvz,-I,4,-O,2;retime;&get,-n;&st;&dch;&nf;&put;stime,-p;print_stats,-m"
        ),
    ),
    (
        "delay_1",
        {"legacy_rewrite": True, "sizing": True},
        (
            "+fx;mfs;strash;drf,-l;balance;rewrite;drf,-l;balance;rewrite;rewrite,-z;balance;"
            "drf,-l,-z;rewrite,-z;balance;retime,-M,6;scleanup;fraig_store;balance;fraig_store;"
            "balance;rewrite;drf,-l;balance;rewrite;rewrite,-z;balance;drf,-l,-z;rewrite,-z;"
            "balance;fraig_store;balance;rewrite;drf,-l;balance;rewrite;rewrite,-z;balance;"
            "drf,-l,-z;rewrite,-z;balance;fraig_store;balance;rewrite;drf,-l;balance;rewrite;"
            "rewrite,-z;balance;drf,-l,-z;rewrite,-z;balance;fraig_store;fraig_restore;"
            "map,-p,-B,0.2,-A,0.9,-M,0;fraig_store;balance;fraig_store;balance;rewrite;drf,-l;"
            "balance;rewrite;rewrite,-z;balance;drf,-l,-z;rewrite,-z;balance;fraig_store;balance;"
            "rewrite;drf,-l;balance;rewrite;rewrite,-z;balance;drf,-l,-z;rewrite,-z;balance;"
            "fraig_store;balance;rewrite;drf,-l;balance;rewrite;rewrite,-z;balance;drf,-l,-z;"
            "rewrite,-z;balance;fraig_store;fraig_restore;map;retime;&get,-n;&st;&dch;&nf;&put;"
            "upsize;dnsize;stime,-p;print_stats,-m"
        ),
    ),
    (
        "area_2",
        {"legacy_refactor": True, "legacy_rewrite": True, "area_mapper": "nf"},
        (
            "+fx;mfs;strash;refactor;fraig_store;balance;fraig_store;balance;rewrite;refactor;"
            "balance;rewrite;rewrite,-z;balance;refactor,-z;rewrite,-z;balance;fraig_store;"
            "balance;rewrite;refactor;balance;rewrite;rewrite,-z;balance;refactor,-z;rewrite,-z;"
            "balance;fraig_store;balance;rewrite;refactor;balance;rewrite;rewrite,-z;balance;"
            "refactor,-z;rewrite,-z;balance;fraig_store;fraig_restore;retime,-M,5;scleanup;"
            "fraig_store;balance;fraig_store;balance;rewrite;refactor;balance;rewrite;rewrite,-z;"
            "balance;refactor,-z;rewrite,-z;balance;fraig_store;balance;rewrite;refactor;balance;"
            "rewrite;rewrite,-z;balance;refactor,-z;rewrite,-z;balance;fraig_store;balance;"
            "rewrite;refactor;balance;rewrite;rewrite,-z;balance;refactor,-z;rewrite,-z;balance;"
            "fraig_store;fraig_restore;&get,-n;&nf,-R,1000;&put;fraig_store;balance;fraig_store;"
            "balance;rewrite;refactor;balance;rewrite;rewrite,-z;balance;refactor,-z;rewrite,-z;"
            "balance;fraig_store;balance;rewrite;refactor;balance;rewrite;rewrite,-z;balance;"
            "refactor,-z;rewrite,-z;balance;fraig_store;balance;rewrite;refactor;balance;rewrite;"
            "rewrite,-z;balance;refactor,-z;rewrite,-z;balance;fraig_store;fraig_restore;&get,-n;"
            "&nf,-R,1000;&put;retime;&get,-n;&st;&dch;&nf;&put;stime,-p;print_stats,-m"
        ),
    ),
]

COMPOSED_NAMES = tuple(COMPOSED)
AREA_FAMILY = ("area_0", "area_1", "area_2")

#: The knobs of the composed strategies with the value that is not the default.
KNOBS = {
    "buffering": True,
    "sizing": True,
    "max_fanout": 8,
    "max_slew": "150 ps",
    "mfs3": True,
    "area_mapper": "nf",
    "legacy_refactor": True,
    "legacy_rewrite": True,
}


# ------------------------------------------------------------------------------ the model


def test_the_abc_leaves_have_the_defaults_that_keep_the_script_as_it_was():
    abc = Yosys.Settings().abc
    assert abc.strategy == "area"
    assert (abc.buffering, abc.sizing, abc.mfs3) == (False, False, False)
    assert (abc.max_fanout, abc.max_slew) == (None, None)
    assert abc.area_mapper == "amap"
    assert (abc.legacy_refactor, abc.legacy_rewrite) == (False, False)
    assert abc.clock_derating == 0
    assert (abc.keep_ff, abc.liberty_args) == (False, None)


def test_the_strategies_are_the_documented_values():
    assert set(get_args(asic_groups.AbcGroup.model_fields["strategy"].annotation)) == {
        "area",
        "speed",
        "speed_roundtrip",
        "yosys",
        *COMPOSED_NAMES,
    }
    assert set(get_args(asic_groups.AbcGroup.model_fields["area_mapper"].annotation)) == {
        "amap",
        "nf",
    }
    assert asic_groups.COMPOSED_STRATEGIES == COMPOSED_NAMES


@pytest.mark.parametrize(
    "given,mentions",
    [
        ({"max_fanout": 0}, "max_fanout"),
        ({"max_slew": "0 ps"}, "max_slew"),
        ({"clock_derating": -0.1}, "clock_derating"),
        ({"clock_derating": 1}, "clock_derating"),
        ({"area_mapper": "map"}, "area_mapper"),
        ({"liberty_args": ""}, "liberty_args"),
        ({"strategy": "area_3"}, "strategy"),
        ({"strategy": "delay_4"}, "strategy"),
        ({"strategy": "AREA 0"}, "strategy"),
        ({"sizing_options": {}}, "sizing_options"),
        ({"dress": 1.5}, "dress"),
        ({"exe": "yosys-abc"}, "exe"),
        ({"script": "x.abc"}, "script"),
    ],
)
def test_an_abc_leaf_refuses_a_value_outside_its_type_and_a_row_not_taken_is_no_setting(
    given, mentions
):
    with pytest.raises(Exception, match=mentions):
        Yosys.Settings.from_input({"abc": given})


def test_the_new_abc_leaves_have_a_tier_and_a_description():
    group = asic_groups.AbcGroup
    for leaf, info in group.model_fields.items():
        assert info.description and tier_of(group, leaf) is not None, leaf


# ------------------------------------------------------------------------------ the scripts


@pytest.mark.parametrize("strategy", COMPOSED_NAMES)
def test_a_composed_strategy_is_the_script_of_librelane(strategy):
    assert abc_strategy_script(strategy) == COMPOSED[strategy]


@pytest.mark.parametrize(
    "strategy,knobs,script",
    COMPOSED_WITH_KNOBS,
    ids=[f"{strategy}-{'-'.join(knobs)}" for strategy, knobs, _ in COMPOSED_WITH_KNOBS],
)
def test_a_composed_strategy_with_knobs_is_the_script_of_librelane_with_the_same_options(
    strategy, knobs, script
):
    assert abc_strategy_script(strategy, Composition(**knobs)) == script


def test_buffering_without_a_limit_leaves_the_limits_to_abc():
    script = abc_strategy_script("area_0", Composition(buffering=True))
    assert script.endswith("&put;buffer;upsize;dnsize;stime,-p;print_stats,-m")
    script = abc_strategy_script("area_0", Composition(buffering=True, max_slew=120))
    assert ";buffer,-S,120;upsize;dnsize;" in script


def test_the_composed_scripts_end_with_the_commands_that_report_the_result():
    for strategy in COMPOSED_NAMES:
        assert COMPOSED[strategy].endswith(";stime,-p;print_stats,-m")
        assert "{D}" not in COMPOSED[strategy]


def test_a_strategy_name_that_is_no_strategy_is_a_bug():
    with pytest.raises(ValueError, match="unknown abc strategy"):
        abc_strategy_script("area_9")


@SCRIPT_FORMATS
@pytest.mark.parametrize("strategy", COMPOSED_NAMES)
def test_each_composed_strategy_reaches_the_abc_command_in_both_script_formats(
    tmp_path, monkeypatch, script_format, strategy
):
    commands = _run(tmp_path, monkeypatch, script_format, abc={"strategy": strategy})
    assert _abc_script(commands) == COMPOSED[strategy]
    (abc,) = _words(commands, "abc")
    assert abc[abc.index("-script") + 2 :][0] == "-liberty"


@SCRIPT_FORMATS
@pytest.mark.parametrize(
    "strategy,knobs,script",
    COMPOSED_WITH_KNOBS,
    ids=[f"{strategy}-{'-'.join(knobs)}" for strategy, knobs, _ in COMPOSED_WITH_KNOBS],
)
def test_the_knobs_reach_the_abc_command_in_both_script_formats(
    tmp_path, monkeypatch, script_format, strategy, knobs, script
):
    given = {name: f"{value} ps" if name == "max_slew" else value for name, value in knobs.items()}
    commands = _run(tmp_path, monkeypatch, script_format, abc={"strategy": strategy, **given})
    assert _abc_script(commands) == script


@pytest.mark.parametrize("knob", sorted(KNOBS))
def test_every_knob_changes_the_script_of_a_strategy_that_reads_it(knob):
    reads = {
        "buffering": "area_0",
        "sizing": "area_0",
        "max_fanout": "area_0",
        "max_slew": "area_0",
        "mfs3": "area_0",
        "area_mapper": "area_0",
        "legacy_refactor": "delay_0",
        "legacy_rewrite": "delay_0",
    }[knob]
    given = {knob: KNOBS[knob]}
    if knob in ("max_fanout", "max_slew"):
        given["buffering"] = True
    if "max_slew" in given:
        given["max_slew"] = round(Time(given["max_slew"]).to("ps"))
    assert abc_strategy_script(reads, Composition(**given)) != abc_strategy_script(reads)


# ------------------------------------------------------------------------------ the other leaves


@SCRIPT_FORMATS
@pytest.mark.parametrize(
    "target,derating,expected",
    [("auto", 0.25, "1500.000"), ("auto", 0, "2000.000"), ("auto", 0.5, "1000.000")],
)
def test_the_clock_derating_shortens_the_period_that_abc_gets(
    tmp_path, monkeypatch, script_format, target, derating, expected
):
    commands = _run(
        tmp_path,
        monkeypatch,
        script_format,
        abc={"delay_target": target, "clock_derating": derating},
    )
    (abc,) = _words(commands, "abc")
    assert abc[abc.index("-D") + 1] == expected


@SCRIPT_FORMATS
def test_liberty_args_is_a_flag_of_the_abc_command_before_the_extra_flags(
    tmp_path, monkeypatch, script_format
):
    commands = _run(
        tmp_path,
        monkeypatch,
        script_format,
        abc_flags=["-nocleanup"],
        abc={"liberty_args": "-G 250"},
    )
    (abc,) = _words(commands, "abc")
    argument = abc[abc.index("-liberty_args") + 1]
    assert argument in ("-G", '"-G') if script_format == "ys" else argument == "-G 250"
    assert abc.index("-liberty_args") < abc.index("-nocleanup")
    assert abc.count("-liberty_args") == 1


@SCRIPT_FORMATS
def test_the_default_abc_command_has_none_of_the_new_flags(tmp_path, monkeypatch, script_format):
    commands = _run(tmp_path, monkeypatch, script_format)
    (abc,) = _words(commands, "abc")
    for flag in ("-liberty_args", "-keepff", "-dff", "-dress"):
        assert flag not in abc


@SCRIPT_FORMATS
def test_dress_is_a_flag_of_the_abc_command_before_the_extra_flags(
    tmp_path, monkeypatch, script_format
):
    commands = _run(
        tmp_path, monkeypatch, script_format, abc_flags=["-nocleanup"], abc={"dress": True}
    )
    (abc,) = _words(commands, "abc")
    assert abc.count("-dress") == 1
    assert abc.index("-dress") < abc.index("-nocleanup")


def test_dress_is_off_by_default_and_an_expert_leaf_that_names_the_open_issues():
    assert asic_groups.AbcGroup().dress is False
    assert tier_of(asic_groups.AbcGroup, "dress") == "expert"
    description = asic_groups.AbcGroup.model_fields["dress"].description
    assert description.startswith("Warning:")
    for issue in ("YosysHQ/yosys#3644", "berkeley-abc/abc#445"):
        assert issue in description


@SCRIPT_FORMATS
def test_dress_goes_to_the_main_abc_command_and_not_to_the_retiming_run(
    tmp_path, monkeypatch, script_format
):
    commands = _run(
        tmp_path,
        monkeypatch,
        script_format,
        abc={"dress": True},
        sequential={"retime_modules": ["mac"]},
    )
    retime, main = _words(commands, "abc")
    assert "-dress" not in retime and "-dress" in main


def test_abc_new_takes_no_dress():
    """`abc_new` has no `-dress` option, so `abc.dress: true` cannot go with it."""
    problems = _keys(abc={"mapper": "abc_new", "dress": True})
    message = problems["abc.dress"]
    assert "`abc.dress`" in message and "`abc.mapper: abc_new`" in message
    Yosys.check_settings_supported(
        Yosys.Settings(
            platform="nangate45",
            abc={"mapper": "abc_new", "dress": False},
        )
    )


@SCRIPT_FORMATS
def test_keep_ff_is_a_flag_of_the_retiming_run_of_abc(tmp_path, monkeypatch, script_format):
    commands = _run(
        tmp_path,
        monkeypatch,
        script_format,
        abc={"keep_ff": True},
        sequential={"retime_modules": ["mac"]},
    )
    retime, main = _words(commands, "abc")
    assert retime[:3] == ["abc", "-dff", "-keepff"]
    assert "-keepff" not in main


# ------------------------------------------------------------------------------ the refusals


def _keys(**given: Any) -> dict[str, str]:
    return dict(_refused(platform="nangate45", **given))


@pytest.mark.parametrize("knob", sorted(KNOBS))
def test_a_knob_is_refused_with_a_strategy_outside_the_composed_family(knob):
    given = {knob: KNOBS[knob]}
    if knob in ("max_fanout", "max_slew"):
        given["buffering"] = True
    for strategy in ("area", "speed", "speed_roundtrip", "yosys"):
        problems = _keys(abc={"strategy": strategy, **given})
        message = problems[f"abc.{knob}"]
        assert f"`abc.{knob}`" in message and "composed" in message, strategy
        assert f"`abc.strategy: {strategy}`" in message


@pytest.mark.parametrize("knob", sorted(KNOBS))
def test_a_knob_is_refused_with_an_abc_script(knob):
    given = {knob: KNOBS[knob]}
    if knob in ("max_fanout", "max_slew"):
        given["buffering"] = True
    problems = _keys(abc_script="+strash;map", abc=given)
    assert f"`abc.{knob}`" in problems[f"abc.{knob}"]


@pytest.mark.parametrize("strategy", COMPOSED_NAMES)
def test_a_knob_is_accepted_with_a_strategy_that_reads_it(strategy):
    given = {"buffering": True, "max_fanout": 8, "legacy_refactor": True}
    given["mfs3"] = True
    if strategy in AREA_FAMILY or strategy == "delay_3":
        given["area_mapper"] = "nf"
    Yosys.check_settings_supported(
        Yosys.Settings(platform="nangate45", abc={"strategy": strategy, **given})
    )


@SCRIPT_FORMATS
@pytest.mark.parametrize("strategy", COMPOSED_NAMES)
def test_mfs3_is_accepted_with_every_composed_strategy_and_reaches_the_script(
    tmp_path, monkeypatch, script_format, strategy
):
    settings = Yosys.Settings(platform="nangate45", abc={"strategy": strategy, "mfs3": True})
    Yosys.check_settings_supported(settings)
    commands = _run(tmp_path, monkeypatch, script_format, abc={"strategy": strategy, "mfs3": True})
    assert ";mfs3," in _abc_script(commands)


def test_the_description_of_mfs3_warns_and_states_what_was_measured():
    description = asic_groups.AbcGroup.model_fields["mfs3"].description
    assert description.startswith("Warning:")
    for fact in (
        "`Abc_TtExpand`",
        "OSS CAD Suite yosys 0.69+286 and 0.70+179",
        "Homebrew yosys 0.69+post",
        "Commit a4fb2d39c of berkeley-abc/abc fixes it",
        "yosys 0.68 and 0.66",
        "`mfs3 -e -m`",
        "`src/opt/sfm/sfmDec.c`",
        "not equivalent",
        "0.2%",
    ):
        assert fact in description, fact
    assert "http" not in description and "#" not in description
    assert tier_of(asic_groups.AbcGroup, "mfs3") == "expert"


def test_the_limits_of_buffering_need_buffering():
    for leaf, value in (("max_fanout", 8), ("max_slew", "150 ps")):
        problems = _keys(abc={"strategy": "area_0", leaf: value})
        message = problems[f"abc.{leaf}"]
        assert f"`abc.{leaf}`" in message and "`abc.buffering`" in message


def test_sizing_has_no_effect_where_buffering_sizes_too():
    problems = _keys(abc={"strategy": "area_0", "buffering": True, "sizing": True})
    message = problems["abc.sizing"]
    assert "`abc.sizing`" in message and "`abc.buffering: true`" in message


@pytest.mark.parametrize("strategy", ["delay_0", "delay_1", "delay_2"])
def test_the_new_area_mapper_is_refused_where_the_strategy_has_no_area_mapping(strategy):
    problems = _keys(abc={"strategy": strategy, "area_mapper": "nf"})
    message = problems["abc.area_mapper"]
    assert f"`abc.strategy: {strategy}`" in message and "`area_0`" in message


@pytest.mark.parametrize("target", [None, "1500 ps"])
def test_the_clock_derating_needs_the_auto_delay_target(target):
    problems = _keys(abc={"delay_target": target, "clock_derating": 0.1})
    message = problems["abc.clock_derating"]
    assert "`abc.clock_derating`" in message and "`abc.delay_target: auto`" in message


def test_abc_new_takes_no_liberty_args():
    problems = _keys(abc={"mapper": "abc_new", "liberty_args": "-G 250"})
    message = problems["abc.liberty_args"]
    assert "`abc.liberty_args`" in message and "`abc.mapper: abc_new`" in message


def test_abc_new_takes_no_dff():
    """`abc_new` has no `-dff` option, and the liberty mapping that it belongs to maps the
    flip-flops before it runs, so `abc_dff: true` is refused with it and `false` is accepted."""
    problems = _keys(abc_dff=True, abc={"mapper": "abc_new"})
    assert "`abc_dff`" in problems["abc_dff"]
    Yosys.check_settings_supported(
        Yosys.Settings(platform="nangate45", abc_dff=False, abc={"mapper": "abc_new"})
    )


def test_keep_ff_acts_on_the_retiming_run_only():
    problems = _keys(abc={"keep_ff": True})
    message = problems["abc.keep_ff"]
    assert "`abc.keep_ff`" in message and "`sequential.retime_modules`" in message
    Yosys.check_settings_supported(
        Yosys.Settings(
            platform="nangate45",
            abc={"keep_ff": True},
            sequential={"retime_modules": ["top"]},
        )
    )


@pytest.mark.parametrize(
    "flag,key",
    [
        ("-keepff", "abc.keep_ff"),
        ("-dress", "abc.dress"),
        ("-liberty_args", "abc.liberty_args"),
        ("-D", "abc.delay_target"),
        ("-script", "abc_script"),
        ("-constr", "abc_constr"),
        ("-liberty", "liberty"),
        ("-dont_use", "dont_use_cells"),
    ],
)
def test_an_abc_flag_that_a_setting_owns_is_refused_naming_the_setting(flag, key):
    problems = _keys(abc_flags=[f"{flag} x"])
    assert f"`{key}`" in problems["abc_flags"] and f"`{flag}`" in problems["abc_flags"]


@pytest.mark.parametrize("flag", ["-g", "-lut", "-luts"])
def test_a_generic_mapping_flag_in_abc_flags_is_refused_saying_that_the_mapping_maps_to_cells(flag):
    message = _keys(abc_flags=[f"{flag} x"])["abc_flags"]
    assert f"`{flag}`" in message and "maps to cells" in message
    assert "`gates`" not in message and "`lut`" not in message


def test_dff_in_abc_flags_is_refused_saying_that_it_has_no_effect_with_a_liberty_mapping():
    message = _keys(abc_flags=["-dff"])["abc_flags"]
    assert "`-dff`" in message and "no effect" in message and "dfflibmap" in message
    assert "abc_dff" not in message


def test_other_abc_flags_are_accepted():
    Yosys.check_settings_supported(
        Yosys.Settings(
            platform="nangate45", abc_flags=["-nocleanup", "-showtmp", "-markgroups", "-fast"]
        )
    )


@pytest.mark.parametrize(
    "key,given",
    [
        ("abc.buffering", {"abc": {"strategy": "area_0", "buffering": True}}),
        ("abc.clock_derating", {"abc": {"delay_target": "auto", "clock_derating": 0.1}}),
        ("abc.keep_ff", {"abc": {"keep_ff": True}}),
        ("abc.liberty_args", {"abc": {"liberty_args": "-G 250"}}),
        ("abc.strategy", {"abc": {"strategy": "delay_1"}}),
    ],
)
@pytest.mark.parametrize("mapping", [{"gates": ["AND"]}, {}], ids=str)
def test_a_new_abc_leaf_is_refused_when_no_liberty_mapping_runs(key, given, mapping):
    problems = dict(_refused(**given, **mapping))
    assert no_liberty_message(key, mapping) == problems[key]


# ------------------------------------------------------------------------------ real yosys


def _real(script_format: str) -> None:
    if script_format == "ys":
        require_yosys()
    else:
        require_yosys_tcl()


FORMATS_REAL = pytest.mark.parametrize("script_format", ["ys", "tcl"])


def _run_real(tmp_path, script_format: str, enables: bool = False, **given: Any):
    """Run `yosys` on the `tiny` platform and the multiplier-accumulator, or on the design with
    enables when `enables` is set."""
    design = _enable_design(tmp_path / "design") if enables else _design(tmp_path)
    settings = _tiny(tmp_path, **given)
    with contextlib.suppress(FlowException), forced_script_format(script_format):
        DefaultRunner(tmp_path / "xeda_run").run_flow(Yosys, design, settings)
    (results,) = tmp_path.glob("xeda_run/*/yosys/results.json")
    assert json.loads(results.read_text())["outcome"] == "passed", results.read_text()
    assert (results.parent / f"yosys_synth.{script_format}").is_file()
    return results.parent


@FORMATS_REAL
@pytest.mark.parametrize("strategy", COMPOSED_NAMES)
def test_real_yosys_maps_with_each_composed_strategy(tmp_path, script_format, strategy):
    _real(script_format)
    run = _run_real(tmp_path, script_format, abc={"strategy": strategy})
    log = (run / "yosys.log").read_text()
    assert "ABC: + print_stats -m" in log
    assert "ABC RESULTS" in log


@FORMATS_REAL
def test_real_yosys_maps_with_a_composed_strategy_and_its_options(tmp_path, script_format):
    _real(script_format)
    run = _run_real(
        tmp_path,
        script_format,
        abc={
            "strategy": "delay_3",
            "buffering": True,
            "max_fanout": 8,
            "max_slew": "150 ps",
            "area_mapper": "nf",
            "legacy_refactor": True,
            "legacy_rewrite": True,
        },
    )
    log = (run / "yosys.log").read_text()
    assert "ABC: + buffer -N 8 -S 150" in log and "ABC: + &nf -R 1000" in log
    assert "ABC: + refactor " in log and "ABC: + rewrite " in log


@FORMATS_REAL
def test_real_yosys_sizes_the_cells_of_a_composed_strategy(tmp_path, script_format):
    _real(script_format)
    run = _run_real(tmp_path, script_format, abc={"strategy": "delay_1", "sizing": True})
    log = (run / "yosys.log").read_text()
    assert "ABC: + upsize " in log and "ABC: + buffer " not in log.split("ABC: + upsize ")[0]


@FORMATS_REAL
def test_real_yosys_takes_the_mfs3_step_on_a_design_that_it_does_not_stop_on(
    tmp_path, script_format
):
    """`mfs3` can stop ABC on an assertion failure with other strategies and designs (the
    multiplier of the default design with `area_2`, and the delay strategies)."""
    _real(script_format)
    run = _run_real(tmp_path, script_format, enables=True, abc={"strategy": "area_0", "mfs3": True})
    assert "ABC: + mfs3 -aemvz -I 4 -O 2" in (run / "yosys.log").read_text()


@FORMATS_REAL
def test_real_yosys_runs_the_dress_command_of_abc_and_the_mapped_check_judges_the_netlist(
    tmp_path, script_format
):
    """With a Liberty library, `-dress` can map to LUTs (YosysHQ/yosys#3644). The run then stops
    in `check -assert -mapped`, which finds cells that are not library cells. A yosys that does
    not have this bug finishes the run."""
    _real(script_format)
    design = _design(tmp_path)
    settings = _tiny(tmp_path, abc={"dress": True})
    with contextlib.suppress(FlowException):
        DefaultRunner(tmp_path / "xeda_run").run_flow(Yosys, design, settings)
    (results,) = tmp_path.glob("xeda_run/*/yosys/results.json")
    log = (results.parent / "yosys.log").read_text()
    assert "ABC: + dress " in log
    if json.loads(results.read_text())["outcome"] != "passed":
        assert "unmapped internal cell of type $lut" in log
        assert "problems in 'check -assert'" in log


@FORMATS_REAL
def test_real_yosys_takes_the_clock_derating_and_the_liberty_arguments(tmp_path, script_format):
    _real(script_format)
    run = _run_real(
        tmp_path,
        script_format,
        abc={"delay_target": "auto", "clock_derating": 0.25, "liberty_args": "-G 250"},
    )
    log = (run / "yosys.log").read_text()
    assert "abc_liberty_args provided" in log and "ABC: + read_lib" in log
    assert " -G 250 -w " in log
    assert (run / f"yosys_synth.{script_format}").read_text().count("-D") >= 1
    assert "1500.000" in (run / f"yosys_synth.{script_format}").read_text()


@FORMATS_REAL
def test_real_yosys_keeps_the_names_of_the_retimed_flip_flops(tmp_path, script_format):
    _real(script_format)
    run = _run_real(
        tmp_path,
        script_format,
        abc={"keep_ff": True},
        sequential={"retime_modules": ["mac"]},
    )
    assert "ABC: + retime -M 5 -v -o" in (run / "yosys.log").read_text()
