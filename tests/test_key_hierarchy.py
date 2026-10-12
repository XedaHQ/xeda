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


def test_a_table_given_as_a_mapping_still_takes_keys_from_a_dotted_key():
    """Only a value and a table conflict. A key set again keeps the last value, a dotted key
    reaches into a table given as a mapping, and two dotted keys share their tables."""
    assert expand_hierarchy({"a": {"b": 1}, "a.c": 2}) == {"a": {"b": 1, "c": 2}}
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
    dotted = {"vivado_synth": {"clock.period": 5.0}, "vivado_synth.fpga.part": "p"}
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
