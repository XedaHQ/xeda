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
