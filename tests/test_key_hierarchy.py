"""A key cannot hold a value and have keys inside it.

`-s timing=true timing.x=1`, a `flows` table with `verilator: 3` beside `"verilator.timing": true`,
a design file with `tb: 3` beside `tb.top: x`: a dotted key makes a table of the keys before its
last part, and a key that was a value cannot be made one. That ended in a traceback
(`'str' object does not support item assignment`) when the value came first, and silently lost
the table when it came last. It is one mistake, reported the same way in both orders, naming both
keys, by the one function every dotted key goes through (`utils.set_hierarchy`).
"""

import pytest

from xeda.utils import (
    ConflictingKeys,
    XedaException,
    expand_hierarchy,
    set_hierarchy,
    settings_to_dict,
)

VALUES = ["3", "true", "x", "", 3, 2.5, True, False, [1, 2], ["a"]]


def test_the_error_is_reported_by_the_command_line_and_by_a_validator_alike():
    """A `XedaException` is what the command line reports without a traceback. A `ValueError` is
    what a validator turns into a field error."""
    assert issubclass(ConflictingKeys, XedaException) and issubclass(ConflictingKeys, ValueError)


@pytest.mark.parametrize("value", VALUES, ids=repr)
@pytest.mark.parametrize("value_first", [True, False], ids=["value-first", "child-first"])
def test_a_value_and_a_key_inside_it_conflict_in_either_order(value, value_first):
    items = [("timing", value), ("timing.x", 1)]
    if not value_first:
        items.reverse()
    table: dict = {}
    with pytest.raises(ConflictingKeys) as raised:
        for key, item in items:
            set_hierarchy(table, key, item)

    assert "`timing` is set to a value, and `timing.x` sets a key inside it" in str(raised.value)


@pytest.mark.parametrize("value_first", [True, False], ids=["value-first", "child-first"])
def test_the_text_of_the_command_line_and_a_mapping_conflict_alike(value_first):
    pair = ["timing=true", "timing.x=1"]
    mapping = {"timing": "true", "timing.x": 1}
    if not value_first:
        pair.reverse()
        mapping = dict(reversed(mapping.items()))

    for build in (lambda: settings_to_dict(pair), lambda: expand_hierarchy(mapping)):
        with pytest.raises(ConflictingKeys, match="`timing` is set to a value.*`timing.x`"):
            build()


@pytest.mark.parametrize("value", VALUES, ids=repr)
def test_a_table_given_as_a_whole_after_a_value_conflicts_too(value):
    """A later layer that gives the key a whole table (a mapping, not a dotted key) is the same
    mistake: before, the table silently replaced the value."""
    table: dict = {}
    set_hierarchy(table, "timing", value)
    with pytest.raises(ConflictingKeys, match=r"`timing` is set to a value, and `timing.x`"):
        set_hierarchy(table, "timing", {"x": 1})
    assert table == {"timing": value}
    with pytest.raises(ConflictingKeys):
        settings_to_dict([{"timing": value}, {"timing": {"x": 1}}])


def test_the_keys_are_named_in_full_wherever_they_are_deep():
    with pytest.raises(ConflictingKeys) as deep:
        settings_to_dict(["flows.verilator.clock=3", "flows.verilator.clock.period=5"])
    with pytest.raises(ConflictingKeys) as nested:
        expand_hierarchy({"flows": {"verilator": {"clock": 3, "clock.period": 5}}})

    assert "`flows.verilator.clock` is set to a value, and `flows.verilator.clock.period`" in str(
        deep.value
    )
    assert "`flows.verilator.clock` is set to a value, and `flows.verilator.clock.period`" in str(
        nested.value
    )


def test_dotted_keys_share_their_tables_and_a_key_set_again_keeps_the_last_value():
    assert expand_hierarchy({"a.b": 1, "a.c": 2}) == {"a": {"b": 1, "c": 2}}
    assert settings_to_dict(["a.b=1", "a.c=2", "a.b=3"]) == {"a": {"b": "3", "c": "2"}}


def test_a_key_given_twice_keeps_the_last_value():
    assert settings_to_dict(["a=1", "a=2"]) == {"a": "2"}
    assert settings_to_dict([{"a": 1}, {"a": [2]}]) == {"a": [2]}


def test_an_empty_key_is_nothing_and_a_table_goes_with_it_in_either_order():
    """`None` is how a file writes a key it leaves empty: no value, so a table can follow it, or
    it can follow the table. An empty table holds nothing that a value could lose."""
    assert expand_hierarchy({"a": None, "a.b": 1}) == {"a": {"b": 1}}
    assert expand_hierarchy({"a.b": 1, "a": None}) == {"a": {"b": 1}}
    assert settings_to_dict([{"a": {}}, {"a": 2}]) == {"a": 2}
    assert settings_to_dict([{"a": 2}, {"a": {}}]) == {"a": {}}


def test_a_failed_conflict_changes_nothing_that_was_set():
    table: dict = {}
    set_hierarchy(table, "a", 1)
    set_hierarchy(table, "b.c", 2)
    with pytest.raises(ConflictingKeys):
        set_hierarchy(table, "a.d", 3)
    with pytest.raises(ConflictingKeys):
        set_hierarchy(table, "b", 3)

    assert table == {"a": 1, "b": {"c": 2}}


# ------------------------------------------------------------ a table written both ways
#
# `fpga.part` and `fpga: {...}` in one mapping set keys of the same table. The expansion used to
# keep whichever came last (`{"fpga.part": "P", "fpga": {"speed": -6}}` lost the part, and the
# reverse order kept both), so the result depended on the order of the keys and nothing said so.

WRITTEN_BOTH_WAYS = (
    "`{dotted}` and `{table}: {{...}}` both set keys of `{table}`: write one of them"
)


@pytest.mark.parametrize("table_first", [True, False], ids=["table-first", "dotted-first"])
def test_a_table_written_dotted_and_as_a_table_is_refused_in_either_order(table_first):
    items = [("fpga", {"speed": -6}), ("fpga.part", "P")]
    if not table_first:
        items.reverse()

    with pytest.raises(ConflictingKeys) as raised:
        expand_hierarchy(dict(items))

    assert str(raised.value) == WRITTEN_BOTH_WAYS.format(dotted="fpga.part", table="fpga")
    assert raised.value.value_key == "fpga"


@pytest.mark.parametrize(
    "mapping, dotted, table",
    [
        ({"fpga.part": "P", "fpga": {}}, "fpga.part", "fpga"),
        ({"a.b.c": 1, "a.b": {"d": 2}}, "a.b.c", "a.b"),
        ({"a.b.c": 1, "a": {"b": {"d": 2}}}, "a.b.c", "a"),
        ({"x": {"fpga.part": "P", "fpga": {"speed": -6}}}, "x.fpga.part", "x.fpga"),
        (
            {"flows": {"v": {"fpga": {"speed": 1}, "fpga.part": "P"}}},
            "flows.v.fpga.part",
            "flows.v.fpga",
        ),
    ],
    ids=["empty-table", "deep-dotted", "table-above", "nested", "flows"],
)
def test_the_error_names_both_spellings_in_full_wherever_they_are_deep(mapping, dotted, table):
    with pytest.raises(ConflictingKeys) as raised:
        expand_hierarchy(mapping)

    assert str(raised.value) == WRITTEN_BOTH_WAYS.format(dotted=dotted, table=table)


def test_a_table_written_dotted_and_as_a_table_is_refused_inside_a_value_set_by_a_path():
    """`set_hierarchy` takes a mapping as a value, as `-s` and an API caller's list of mappings do."""
    with pytest.raises(ConflictingKeys, match="`a.fpga.part` and `a.fpga: {...}`"):
        set_hierarchy({}, "a", {"fpga.part": "P", "fpga": {"speed": -6}})
    with pytest.raises(ConflictingKeys, match="`fpga.part` and `fpga: {...}`"):
        settings_to_dict([{"fpga.part": "P", "fpga": {"speed": -6}}])
    with pytest.raises(ConflictingKeys, match="`fpga.part` and `fpga: {...}`"):
        settings_to_dict({"fpga.part": "P", "fpga": {"speed": -6}})


def test_the_normal_forms_are_not_refused():
    assert expand_hierarchy({"fpga.part": "P", "fpga.speed": -6}) == {
        "fpga": {"part": "P", "speed": -6}
    }
    assert expand_hierarchy({"fpga": {"part": "P", "speed": -6}}) == {
        "fpga": {"part": "P", "speed": -6}
    }
    assert expand_hierarchy({"a": {"b": {"c": 1}}, "d": {"e.f": 2, "e.g": 3}}) == {
        "a": {"b": {"c": 1}},
        "d": {"e": {"f": 2, "g": 3}},
    }
    assert expand_hierarchy({"a.b": {"c": 1}, "a.d": {"e": 2}}) == {
        "a": {"b": {"c": 1}, "d": {"e": 2}}
    }
    assert expand_hierarchy({"fpga": None, "fpga.part": "P"}) == {"fpga": {"part": "P"}}


def test_separate_mappings_are_separate_origins_and_merge_by_precedence():
    """`-s fpga.part=P` and an API mapping `{fpga: {...}}` are two origins, not one mapping."""
    assert settings_to_dict(["fpga.part=P", {"fpga": {"speed": -6}}]) == {"fpga": {"speed": -6}}
    assert settings_to_dict([{"fpga": {"speed": -6}}, "fpga.part=P"]) == {
        "fpga": {"speed": -6, "part": "P"}
    }


# ------------------------------------------------------------------ one rule in every origin
#
# A dotted key is a path in every place a design or a flow setting is written: a design file, a
# project's design entry, a target, the design overrides (the command line's and an API caller's),
# and the `flows` of a design and of a project. The same data written with dotted keys and as
# nested tables must give the same design and the same settings.

import json
from pathlib import Path

from xeda.design import Design
from xeda.xedaproject import XedaProject

DESIGN = {
    "language": {"vhdl": {"standard": "2008"}},
    "rtl": {"sources": ["a.vhd"], "top": "a", "clock": {"port": "clk"}},
    "tb": {"top": "tb"},
    "flows": {"vivado_synth": {"fpga": {"part": "xc7a12tcsg325-1"}, "clock": {"period": 5.0}}},
}
#: the same keys, written as dotted keys at every level
DOTTED = {
    "language.vhdl.standard": "2008",
    "rtl.sources": ["a.vhd"],
    "rtl.top": "a",
    "rtl.clock.port": "clk",
    "tb.top": "tb",
    "flows.vivado_synth.fpga.part": "xc7a12tcsg325-1",
    "flows.vivado_synth.clock.period": 5.0,
}
OVERLAY_NESTED = {"tb": {"top": "other"}, "flows": {"vivado_synth": {"clock": {"period": 4.0}}}}
OVERLAY_DOTTED = {"tb.top": "other", "flows.vivado_synth.clock.period": 4.0}


def _dump(design: Design, root: Path) -> dict:
    """The design as data, with the directory it was written in left out of every path."""
    return json.loads(design.model_dump_json().replace(str(root), "ROOT"))


def _root(tmp_path: Path) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    (tmp_path / "a.vhd").write_text("entity a is end;\n")
    return tmp_path


def _design_file(tmp_path: Path, data: dict) -> Design:
    path = _root(tmp_path) / "d.json"
    path.write_text(json.dumps({"name": "d", **data}))
    return Design.from_file(path)


def _project(tmp_path: Path, entry: dict, flows: dict | None = None, **options) -> Design:
    path = _root(tmp_path) / "xedaproject.json"
    path.write_text(json.dumps({"design": [{"name": "d", **entry}], "flows": flows or {}}))
    design = XedaProject.from_file(path, **options).get_design(0)
    assert design is not None
    return design


ORIGINS = {
    "design file": lambda tmp, dotted: _design_file(tmp, DOTTED if dotted else DESIGN),
    "project design entry": lambda tmp, dotted: _project(tmp, DOTTED if dotted else DESIGN),
    "target": lambda tmp, dotted: _design_file(
        tmp,
        {
            **(DESIGN),
            "targets": {"t": OVERLAY_DOTTED if dotted else OVERLAY_NESTED},
        },
    ),
    "design overrides of a design file": lambda tmp, dotted: _overridden_file(tmp, dotted),
    "design overrides of a project": lambda tmp, dotted: _project(
        tmp, DESIGN, design_overrides=OVERLAY_DOTTED if dotted else OVERLAY_NESTED
    ),
    "target in a project design entry": lambda tmp, dotted: _project(
        tmp, {**DESIGN, "targets": {"t": OVERLAY_DOTTED if dotted else OVERLAY_NESTED}}
    ),
}


def _overridden_file(tmp_path: Path, dotted: bool) -> Design:
    path = _root(tmp_path) / "d.json"
    path.write_text(json.dumps({"name": "d", **DESIGN}))
    return Design.from_file(path, overrides=OVERLAY_DOTTED if dotted else OVERLAY_NESTED)


@pytest.mark.parametrize("origin", ORIGINS)
def test_a_dotted_key_means_the_same_in_every_origin_of_a_design(origin, tmp_path):
    nested = _dump(ORIGINS[origin](tmp_path / "nested", False), tmp_path / "nested")
    dotted = _dump(ORIGINS[origin](tmp_path / "dotted", True), tmp_path / "dotted")
    assert dotted == nested


def test_a_dotted_key_in_a_projects_flows_means_the_same(tmp_path):
    nested = {"vivado_synth": {"fpga": {"part": "p"}, "clock": {"period": 5.0}}}
    dotted = {"vivado_synth": {"clock.period": 5.0, "fpga.part": "p"}}
    (tmp_path / "nested").mkdir()
    (tmp_path / "dotted").mkdir()
    flows = {}
    for name, table in (("nested", nested), ("dotted", dotted)):
        path = tmp_path / name / "xedaproject.json"
        path.write_text(json.dumps({"flows": table}))
        flows[name] = XedaProject.from_file(path).flows
    assert flows["dotted"] == flows["nested"]


def test_a_project_reports_a_value_and_a_key_inside_it_as_it_does_in_a_design_file(tmp_path):
    entry = {"tb": 3, "tb.top": "x"}
    with pytest.raises(Exception, match=r"`tb` is set to a value, and `tb.top`"):
        _design_file(tmp_path / "file", entry)
    with pytest.raises(Exception, match=r"`tb` is set to a value, and `tb.top`"):
        _project(tmp_path / "project", entry)


# ------------------------------------------------ a table written both ways, in every origin

import re
import textwrap

from xeda.design import DesignValidationError
from xeda.flow_runner import DefaultRunner
from xeda.xedaproject import ProjectFileError

BOTH_WAYS = r"`{dotted}` and `{table}: \{{\.\.\.\}}` both set keys of `{table}`: write one of them"
#: a table written both ways inside a flow section, the way the report in the field wrote it
FPGA_BOTH = "flows:\n  vivado_synth:\n    fpga.part: xc7a12tcsg325-1\n    fpga:\n      speed: -1\n"


def _yaml_design(directory: Path, text: str = "") -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "top.v").write_text("module top; endmodule\n")
    path = directory / "d.yaml"
    path.write_text(
        'name: d\nrtl:\n  sources: ["top.v"]\n  top: top\n' + textwrap.dedent(text),
        encoding="utf-8",
    )
    return path


@pytest.mark.parametrize("dotted_first", [True, False], ids=["dotted-first", "table-first"])
def test_a_design_file_refuses_a_table_written_both_ways_at_the_key(tmp_path, dotted_first):
    block = "tb.top: tb\ntb:\n  sources: []\n"
    if not dotted_first:
        block = "tb:\n  sources: []\ntb.top: tb\n"
    path = _yaml_design(tmp_path, block)

    with pytest.raises(DesignValidationError, match=BOTH_WAYS.format(dotted="tb.top", table="tb")):
        Design.from_file(path)
    try:
        Design.from_file(path)
    except DesignValidationError as e:
        assert e.file == str(path.absolute())
        assert [location for location, *_ in e.errors] == ["tb"]


def test_a_design_file_refuses_it_inside_a_flow_section(tmp_path):
    path = _yaml_design(tmp_path, FPGA_BOTH)
    table = "flows.vivado_synth.fpga"

    with pytest.raises(
        DesignValidationError, match=BOTH_WAYS.format(dotted=table + r"\.part", table=table)
    ) as raised:
        Design.from_file(path)

    assert [location for location, *_ in raised.value.errors] == [table]


def test_a_design_file_takes_the_normal_forms(tmp_path):
    dotted = _yaml_design(
        tmp_path / "dotted", "flows:\n  vivado_synth:\n    fpga.part: p\n    fpga.speed: -1\n"
    )
    nested = _yaml_design(
        tmp_path / "nested", "flows:\n  vivado_synth:\n    fpga:\n      part: p\n      speed: -1\n"
    )

    assert Design.from_file(dotted).flow == Design.from_file(nested).flow
    assert Design.from_file(dotted).flow["vivado_synth"]["fpga"] == {"part": "p", "speed": -1}


def test_a_target_refuses_it_at_the_target_whichever_is_selected(tmp_path):
    path = _yaml_design(
        tmp_path,
        "targets:\n  a:\n    tb.top: tb\n    tb:\n      sources: []\n  b: {}\n",
    )
    for target in ("a", "b"):
        with pytest.raises(
            DesignValidationError,
            match=BOTH_WAYS.format(dotted=r"targets\.a\.tb\.top", table=r"targets\.a\.tb"),
        ) as raised:
            Design.from_file(path, target=target)
        assert [location for location, *_ in raised.value.errors] == ["targets.a.tb"]


def test_design_overrides_refuse_it(tmp_path):
    path = _yaml_design(tmp_path)

    with pytest.raises(DesignValidationError, match=BOTH_WAYS.format(dotted="tb.top", table="tb")):
        Design.from_file(path, overrides={"tb.top": "tb", "tb": {"sources": []}})


@pytest.mark.parametrize("dotted_first", [True, False], ids=["dotted-first", "table-first"])
def test_a_project_file_refuses_it_naming_the_file(tmp_path, dotted_first):
    flows = ["  vivado_synth.fpga.part: p", "  vivado_synth:", "    fpga:", "      speed: -1"]
    lines = flows if dotted_first else [*flows[1:], flows[0]]
    path = tmp_path / "xedaproject.yaml"
    path.write_text("flows:\n" + "\n".join(lines) + "\n")

    table = r"flows\.vivado_synth"
    both_ways = BOTH_WAYS.format(dotted=table + r"\.fpga\.part", table=table)
    with pytest.raises(ValueError, match=rf"project file {re.escape(str(path))}: {both_ways}"):
        XedaProject.from_file(path)
    design = _yaml_design(tmp_path / "design")
    with pytest.raises(ProjectFileError, match=rf"{re.escape(str(path))}.*{both_ways}"):
        DefaultRunner(tmp_path / "xeda_run", display_results=False).plan(
            "vivado_synth", design, xedaproject=str(path)
        )


def test_a_project_design_entry_refuses_it_at_the_key(tmp_path):
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    path = tmp_path / "xedaproject.yaml"
    path.write_text(
        'designs:\n  - name: d\n    rtl:\n      sources: ["top.v"]\n      top: top\n'
        "    tb.top: tb\n    tb:\n      sources: []\n"
    )

    with pytest.raises(ValueError, match=BOTH_WAYS.format(dotted="tb.top", table="tb")):
        XedaProject.from_file(path).get_design(0)


@pytest.mark.parametrize("origin", ["flow settings", "flow overrides", "design overrides"])
def test_an_api_mapping_refuses_it(tmp_path, origin):
    mapping = {"fpga.part": "p", "fpga": {"speed": -1}}
    if origin == "design overrides":
        mapping = {"tb.top": "tb", "tb": {"sources": []}}
    path = _yaml_design(tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    keyword = {
        "flow settings": "flow_settings",
        "flow overrides": "flow_overrides",
        "design overrides": "design_overrides",
    }[origin]

    with pytest.raises(Exception, match=r"both set keys of `(fpga|tb)`: write one of them"):
        runner.plan("vivado_synth", path, **{keyword: mapping})


def test_the_command_line_takes_dotted_items_and_an_api_table_as_two_origins(tmp_path):
    """`-s` items are separate entries: only a mapping that writes both spellings is refused."""
    path = _yaml_design(tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)

    plan = runner.plan(
        "vivado_synth",
        path,
        flow_settings=["fpga.part=xc7a12tcsg325-1"],
        flow_overrides={"fpga": {"speed": -1}},
    )

    assert plan is not None
