"""The finishing options of the `yosys` ASIC synthesis, and the positions of the user's maps.

The finishing options are the leaves of the group `finish` that act after the mapping: the
removal of formal cells, the names, the single-bit vectors, the checks, and the tie and buffer
cells. The user's techmap files (`techmap_files`) each have a position in the script. Every
rendering test runs for both script formats. The Tcl script runs under `tclsh` with every yosys
command recording itself. The tests at the end run a real yosys on the `tiny` platform.
"""

import json
from pathlib import Path
from typing import Any, get_args

import pytest

from xeda.dataclass import tier_of
from xeda.flow import FlowSettingsError, flowrun_hash
from xeda.flows import Yosys
from xeda.flows.yosys import asic_groups

from .test_yosys_asic import SCRIPT_FORMATS, _design, _words
from .test_yosys_asic_groups import no_liberty_message, _given, _refused, _run, _runner
from .test_yosys_asic_tcl import _render
from .test_yosys_asic_sequential import _real, _run_real
from .tiny_platform import tiny_store
from .tool_utils import use_fake_asic_tools

FINISH_LEAVES = {
    "drop_formal": ("advanced", True),
    "names": ("advanced", "rename_wire"),
    "normalize_single_bit": ("expert", False),
    "check_pre": ("expert", False),
    "allow_tristate": ("expert", False),
    "insert_tie_cells": ("advanced", True),
    "insert_buffers": ("advanced", True),
}

#: The positions of a user's map, in the order of the script.
WHENS = ("pre_synth", "post_coarse", "pre_dff", "post_dff")


def _names(commands: list[list[str]]) -> list[str]:
    return [command[0] for command in commands]


def _map(path: str) -> list[str]:
    return ["techmap", "-map", path]


# ------------------------------------------------------------------------------ the model


def test_the_finish_leaves_have_the_defaults_that_keep_the_script_as_it_was():
    finish = Yosys.Settings().finish
    for name, (_, default) in FINISH_LEAVES.items():
        assert getattr(finish, name) == default, name


def test_the_finish_leaves_have_a_tier_and_a_description():
    fields = asic_groups.FinishGroup.model_fields
    for name, (tier, _) in FINISH_LEAVES.items():
        assert tier_of(asic_groups.FinishGroup, name) == tier, name
        assert fields[name].description, name


def test_the_expert_finish_leaves_start_with_a_warning():
    fields = asic_groups.FinishGroup.model_fields
    for name, (tier, _) in FINISH_LEAVES.items():
        if tier == "expert":
            assert fields[name].description.startswith("Warning:"), name


def test_the_names_are_a_closed_set():
    names = asic_groups.FinishGroup.model_fields["names"].annotation
    assert set(get_args(names)) == {"rename_wire", "autoname", "none"}


@pytest.mark.parametrize(
    "given,mentions",
    [
        ({"names": "wire"}, "names"),
        ({"names": True}, "names"),
        ({"drop_formal": "yes"}, "drop_formal"),
        ({"check_pre": 1}, "check_pre"),
        ({"insert_tie_cells": "no"}, "insert_tie_cells"),
    ],
)
def test_a_finish_leaf_refuses_a_value_outside_its_type(given, mentions):
    with pytest.raises(FlowSettingsError) as refused:
        Yosys.Settings.from_input({"finish": given})
    assert mentions in str(refused.value)


# ------------------------------------------------------------------------------ rendering


@SCRIPT_FORMATS
def test_the_default_finish_runs_the_commands_it_always_ran(tmp_path, monkeypatch, script_format):
    commands = _run(tmp_path, monkeypatch, script_format)
    names = _names(commands)
    fine = names.index("synth", names.index("synth") + 1)
    assert commands[fine + 1 : fine + 5] == [
        ["chformal", "-remove"],
        ["delete", "t:$print"],
        ["rename", "-wire"],
        ["opt", "-purge"],
    ]
    assert _words(commands, "autoname") == [] and _words(commands, "attrmap") == []
    assert _words(commands, "check")[-1] == ["check", "-assert", "-mapped"]
    assert ["check"] not in commands
    assert len(_words(commands, "hilomap")) == 1 and len(_words(commands, "insbuf")) == 1


@SCRIPT_FORMATS
def test_drop_formal_false_keeps_the_formal_cells_and_the_print_cells(
    tmp_path, monkeypatch, script_format
):
    commands = _run(tmp_path, monkeypatch, script_format, finish={"drop_formal": False})
    assert _words(commands, "chformal") == [] and _words(commands, "delete") == []
    assert _words(commands, "rename") == [["rename", "-wire"]]


@SCRIPT_FORMATS
@pytest.mark.parametrize(
    "names,rename,autoname",
    [("rename_wire", 1, 0), ("autoname", 0, 1), ("none", 0, 0)],
)
def test_each_name_choice_renders_its_own_command(
    tmp_path, monkeypatch, script_format, names, rename, autoname
):
    commands = _run(tmp_path, monkeypatch, script_format, finish={"names": names})
    assert _words(commands, "rename") == [["rename", "-wire"]] * rename
    assert _words(commands, "autoname") == [["autoname"]] * autoname


@SCRIPT_FORMATS
def test_autoname_runs_after_the_last_cell_is_made_and_before_the_final_check(
    tmp_path, monkeypatch, script_format
):
    """The cells of `hilomap` and `insbuf` have private names, so `autoname` follows them."""
    commands = _run(
        tmp_path, monkeypatch, script_format, finish={"names": "autoname"}, rmports=True
    )
    names = _names(commands)
    autoname = names.index("autoname")
    assert names.index("insbuf") < names.index("rmports") < autoname
    assert autoname < len(names) - 1 - names[::-1].index("check", 0) + 1
    assert names[autoname + 1] == "check"


@SCRIPT_FORMATS
def test_normalize_single_bit_removes_the_single_bit_vector_attribute_before_the_check(
    tmp_path, monkeypatch, script_format
):
    commands = _run(tmp_path, monkeypatch, script_format, finish={"normalize_single_bit": True})
    names = _names(commands)
    assert _words(commands, "attrmap") == [["attrmap", "-remove", "single_bit_vector"]]
    mapped = next(i for i, command in enumerate(commands) if "-mapped" in command)
    assert names.index("insbuf") < names.index("attrmap") < mapped


@SCRIPT_FORMATS
def test_check_pre_runs_a_plain_check_before_the_first_synth(tmp_path, monkeypatch, script_format):
    commands = _run(tmp_path, monkeypatch, script_format, finish={"check_pre": True})
    names = _names(commands)
    plain = commands.index(["check"])
    assert commands.count(["check"]) == 1
    assert plain < names.index("synth")
    assert names.index("hierarchy") < plain


@SCRIPT_FORMATS
@pytest.mark.parametrize(
    "check_assert,expected",
    [
        (True, ["check", "-assert", "-mapped", "-allow-tbuf"]),
        (False, ["check", "-mapped", "-allow-tbuf"]),
    ],
)
def test_allow_tristate_lets_the_mapped_check_pass_the_tristate_buffers(
    tmp_path, monkeypatch, script_format, check_assert, expected
):
    commands = _run(
        tmp_path,
        monkeypatch,
        script_format,
        check_assert=check_assert,
        finish={"allow_tristate": True},
    )
    mapped = [command for command in _words(commands, "check") if "-mapped" in command]
    assert mapped == [expected]


@SCRIPT_FORMATS
def test_insert_tie_cells_false_runs_no_hilomap(tmp_path, monkeypatch, script_format):
    commands = _run(tmp_path, monkeypatch, script_format, finish={"insert_tie_cells": False})
    assert _words(commands, "hilomap") == [] and len(_words(commands, "insbuf")) == 1


@SCRIPT_FORMATS
def test_insert_buffers_false_runs_no_insbuf(tmp_path, monkeypatch, script_format):
    commands = _run(tmp_path, monkeypatch, script_format, finish={"insert_buffers": False})
    assert _words(commands, "insbuf") == [] and len(_words(commands, "hilomap")) == 1


@SCRIPT_FORMATS
def test_the_cells_the_user_names_are_not_used_while_the_switch_is_on(
    tmp_path, monkeypatch, script_format
):
    commands = _run(
        tmp_path,
        monkeypatch,
        script_format,
        finish={"tie_cells": {"hi": ["H", "Z"], "lo": ["L", "Z"]}, "buffer_cell": ["B", "I", "O"]},
    )
    assert ["hilomap", "-singleton", "-hicell", "H", "Z", "-locell", "L", "Z"] in commands
    assert ["insbuf", "-buf", "B", "I", "O"] in commands


# ------------------------------------------------------------------------------ the rules


def _problems(**given: Any) -> list[tuple[str, str]]:
    return _refused(platform="nangate45", **given)


@pytest.mark.parametrize(
    "switch,cells",
    [
        ("insert_tie_cells", {"tie_cells": {"hi": ["H", "Z"], "lo": ["L", "Z"]}}),
        ("insert_buffers", {"buffer_cell": ["B", "I", "O"]}),
    ],
)
def test_a_cell_is_refused_while_its_switch_is_off(switch, cells):
    (key,) = cells
    problems = dict(_problems(finish={switch: False, **cells}))
    message = problems[f"finish.{key}"]
    assert f"`finish.{key}`" in message and f"`finish.{switch}`" in message


def test_a_cell_with_its_switch_on_is_accepted():
    Yosys.check_settings_supported(
        Yosys.Settings.from_input(
            {"platform": "nangate45", "finish": {"buffer_cell": ["B", "I", "O"]}}
        )
    )


@pytest.mark.parametrize(
    "given",
    [
        {"drop_formal": False},
        {"names": "autoname"},
        {"normalize_single_bit": True},
        {"check_pre": True},
        {"allow_tristate": True},
        {"insert_tie_cells": False},
        {"insert_buffers": False},
    ],
    ids=lambda given: next(iter(given)),
)
@pytest.mark.parametrize("mapping", [{"gates": ["AND"]}, {}], ids=str)
def test_a_finish_leaf_is_refused_when_no_liberty_mapping_runs(given, mapping):
    (name,) = given
    problems = dict(_refused(finish=given, **mapping))
    assert problems[f"finish.{name}"] == no_liberty_message(f"finish.{name}", mapping)


# ------------------------------------------------------------------------------ the techmap files


def test_a_plain_path_is_a_map_at_the_position_it_always_had():
    settings = Yosys.Settings.from_input(
        {"techmap_files": ["a.v", {"path": "b.v", "when": "post_dff"}]}
    )
    assert [(item.path, item.when) for item in settings.techmap_files] == [
        (Path("a.v"), "pre_dff"),
        (Path("b.v"), "post_dff"),
    ]


def test_the_positions_are_a_closed_set_in_script_order():
    when = asic_groups.TechmapFile.model_fields["when"].annotation
    assert get_args(when) == WHENS


@pytest.mark.parametrize(
    "item",
    [
        {"path": "a.v", "when": "late"},
        {"path": "a.v", "when": 3},
        {"when": "pre_dff"},
        {"path": "a.v", "position": "pre_dff"},
        3,
    ],
    ids=str,
)
def test_a_techmap_entry_outside_its_type_is_refused(item):
    with pytest.raises(FlowSettingsError):
        Yosys.Settings.from_input({"techmap_files": [item]})


def test_comma_separated_text_is_a_list_of_plain_paths():
    settings = Yosys.Settings.from_input({"techmap_files": "a.v,b.v"})
    assert [(item.path, item.when) for item in settings.techmap_files] == [
        (Path("a.v"), "pre_dff"),
        (Path("b.v"), "pre_dff"),
    ]


def test_the_settings_reload_from_their_own_dump():
    settings = Yosys.Settings.from_input(
        {"techmap_files": ["a.v", {"path": "b.v", "when": "pre_synth"}]}
    )
    reloaded = Yosys.Settings.from_input(json.loads(settings.model_dump_json()))
    assert reloaded.techmap_files == settings.techmap_files


@SCRIPT_FORMATS
def test_a_plain_path_is_applied_after_the_latch_map_and_before_the_flip_flop_mapping(
    tmp_path, monkeypatch, script_format
):
    (tmp_path / "design").mkdir()
    (tmp_path / "design" / "extra.v").write_text("module _unused(); endmodule\n")
    commands = _run(tmp_path, monkeypatch, script_format, techmap_files=["extra.v"])
    extra = commands.index(_map(str(tmp_path / "design" / "extra.v")))
    names = _names(commands)
    assert names[extra - 1] == "techmap"  # the latch map
    assert names[extra + 1] == "dfflibmap"


def _maps(tmp_path: Path, *names: str) -> list[str]:
    root = tmp_path / "design"
    root.mkdir(exist_ok=True)
    for name in names:
        (root / name).write_text("module _unused(); endmodule\n")
    return [str(root / name) for name in names]


@SCRIPT_FORMATS
def test_each_position_renders_the_map_at_its_point(tmp_path, monkeypatch, script_format):
    paths = dict(zip(WHENS, _maps(tmp_path, *(f"{when}.v" for when in WHENS))))
    commands = _run(
        tmp_path,
        monkeypatch,
        script_format,
        techmap_files=[{"path": f"{when}.v", "when": when} for when in reversed(WHENS)],
    )
    at = {when: commands.index(_map(paths[when])) for when in WHENS}
    names = _names(commands)
    synths = [i for i, name in enumerate(names) if name == "synth"]
    assert at["pre_synth"] + 1 == synths[0]
    assert at["post_coarse"] + 1 == synths[1]
    assert at["pre_dff"] + 1 == names.index("dfflibmap")
    assert names[at["post_dff"] - 1] == "dfflibmap" and names[at["post_dff"] + 1] == "opt"
    assert list(at.values()) == sorted(at.values())


@SCRIPT_FORMATS
def test_maps_at_one_position_keep_the_order_of_the_list(tmp_path, monkeypatch, script_format):
    first, other, last = _maps(tmp_path, "a.v", "b.v", "c.v")
    commands = _run(
        tmp_path,
        monkeypatch,
        script_format,
        techmap_files=[
            {"path": "a.v", "when": "post_dff"},
            {"path": "b.v", "when": "pre_synth"},
            {"path": "c.v", "when": "post_dff"},
        ],
    )
    assert commands.index(_map(first)) + 1 == commands.index(_map(last))
    assert commands.index(_map(other)) < commands.index(_map(first))


@SCRIPT_FORMATS
def test_no_user_map_adds_no_techmap_command_at_the_new_positions(
    tmp_path, monkeypatch, script_format
):
    commands = _run(tmp_path, monkeypatch, script_format)
    names = _names(commands)
    synths = [i for i, name in enumerate(names) if name == "synth"]
    assert names[synths[0] - 1] != "techmap" and names[synths[1] - 1] != "techmap"
    assert names[names.index("dfflibmap") + 1] == "opt"


@SCRIPT_FORMATS
def test_the_coarse_position_follows_the_memory_mapping_and_the_retiming(
    tmp_path, monkeypatch, script_format
):
    (extra,) = _maps(tmp_path, "coarse.v")
    commands = _run(
        tmp_path,
        monkeypatch,
        script_format,
        techmap_files=[{"path": "coarse.v", "when": "post_coarse"}],
        sequential={"retime_modules": ["mac"], "tristate": "logic"},
    )
    names = _names(commands)
    mapped = commands.index(_map(extra))
    assert names.index("select", names.index("tribuf")) < mapped
    assert commands[mapped - 1] == ["select", "-clear"]
    assert names[mapped + 1] == "synth"


@pytest.mark.parametrize("when", WHENS)
def test_the_tcl_script_with_hierarchical_synthesis_keeps_every_position(
    tmp_path, monkeypatch, when
):
    (extra,) = _maps(tmp_path, "m.v")
    _, commands = _render(
        tmp_path,
        monkeypatch,
        techmap_files=[{"path": "m.v", "when": when}],
        hierarchy={"keep_min_cost": 10},
    )
    assert commands.count(_map(extra)) == 1
    names = _names(commands)
    at = commands.index(_map(extra))
    synths = [i for i, name in enumerate(names) if name == "synth"]
    assert len(synths) == 3
    expected = {
        "pre_synth": lambda: at < synths[0],
        "post_coarse": lambda: synths[1] < at < synths[2],
        "pre_dff": lambda: synths[2] < at < names.index("dfflibmap"),
        "post_dff": lambda: names.index("dfflibmap") < at < names.index("abc"),
    }[when]
    assert expected(), (when, names)


@pytest.mark.parametrize("when", WHENS)
def test_the_tcl_script_with_wrapped_operators_keeps_every_position(tmp_path, monkeypatch, when):
    (extra,) = _maps(tmp_path, "m.v")
    _, commands = _render(
        tmp_path,
        monkeypatch,
        techmap_files=[{"path": "m.v", "when": when}],
        arithmetic={"wrap_operators": True},
        abc={"mapper": "abc_new"},
    )
    assert commands.count(_map(extra)) == 1
    names = _names(commands)
    at = commands.index(_map(extra))
    wrap = names.index("wrap_operators")
    if when == "pre_synth":
        assert at < names.index("synth")
    elif when == "post_coarse":
        assert names.index("synth") < at < wrap
    elif when == "pre_dff":
        assert wrap < at < names.index("dfflibmap")
    else:
        assert names.index("dfflibmap") < at < names.index("abc_new")


@pytest.fixture
def fake_yosys(tmp_path, monkeypatch) -> Path:
    use_fake_asic_tools(monkeypatch, tmp_path / "bin")
    return tmp_path


@pytest.mark.parametrize("when", WHENS)
def test_editing_a_map_at_any_position_makes_the_next_launch_stale(fake_yosys, when):
    design = _design(fake_yosys / "design")
    path = design.design_root / "m.v"
    path.write_text("module _unused(); endmodule\n")
    given = _given(tiny_store(fake_yosys), techmap_files=[{"path": "m.v", "when": when}])
    assert _runner(fake_yosys).launch_flow(Yosys, design, given).succeeded
    _, states = _runner(fake_yosys).plan_with_freshness(Yosys, design, flow_settings=given)
    assert states["yosys"].state == "fresh"
    path.write_text("module _unused2(); endmodule\n")
    _, states = _runner(fake_yosys).plan_with_freshness(Yosys, design, flow_settings=given)
    assert states["yosys"].state == "stale"


def test_the_position_of_a_map_is_part_of_the_identity_of_the_run(tmp_path):
    hashes = {
        when: flowrun_hash(
            "yosys",
            Yosys.Settings.from_input({"techmap_files": [{"path": "m.v", "when": when}]}),
            "mac",
        )
        for when in WHENS
    }
    assert len(set(hashes.values())) == len(WHENS)


# ------------------------------------------------------------------------------ real yosys


FORMATS_REAL = pytest.mark.parametrize("script_format", ["ys", "tcl"])


@FORMATS_REAL
@pytest.mark.parametrize(
    "given",
    [
        {"names": "autoname"},
        {"names": "none"},
        {"normalize_single_bit": True},
        {"check_pre": True},
        {"allow_tristate": True},
        {"drop_formal": False},
        {"insert_tie_cells": False, "insert_buffers": False},
    ],
    ids=lambda given: "+".join(given),
)
def test_real_yosys_takes_each_finishing_option(tmp_path, script_format, given):
    _real(script_format)
    run = _run_real(tmp_path, script_format, finish=given)
    log = (run / "yosys.log").read_text()
    if given.get("names") == "autoname":
        assert "Executing AUTONAME pass" in log
    if given.get("normalize_single_bit"):
        assert "Executing ATTRMAP pass" in log
    if given.get("insert_tie_cells") is False:
        assert "Executing HILOMAP pass" not in log
        assert "Executing INSBUF pass" not in log
    if given.get("drop_formal") is False:
        assert "Executing CHFORMAL pass" not in log


@FORMATS_REAL
def test_real_yosys_runs_the_pre_synthesis_check(tmp_path, script_format):
    _real(script_format)
    run = _run_real(tmp_path, script_format, finish={"check_pre": True})
    log = (run / "yosys.log").read_text()
    assert log.count("Executing CHECK pass") >= 3


@FORMATS_REAL
def test_real_yosys_applies_a_map_at_each_position(tmp_path, script_format):
    _real(script_format)
    (tmp_path / "design").mkdir()
    for when in WHENS:
        (tmp_path / "design" / f"{when}.v").write_text("module _unused(); endmodule\n")
    run = _run_real(
        tmp_path,
        script_format,
        techmap_files=[{"path": f"{when}.v", "when": when} for when in WHENS],
    )
    log = (run / "yosys.log").read_text()
    for when in WHENS:
        assert f"{when}.v" in log, when
