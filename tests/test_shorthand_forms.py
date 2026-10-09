"""The shorthand forms of a key that takes a table, found from the models, not listed by hand.

A key may take a table and other forms that each stand for one table (`fpga` as a part number,
`vhdl` as a standard, `cocotb: true`, `parameters` as a list of objects). The merge of two layers
reads both sides as tables, so every form a field accepts has to be one the merge knows: a model's
`as_mapping`, or its `field_shorthands` entry. The sweep walks every field that takes a table, of
the design and of every flow's settings, through nested models. It pairs each with a value of
every structural kind and checks two things.

* A value the field accepts is no mistake (`is_mistake`): it stands for a table, or is a valid form
  that is no table (`WHOLE`, `None` for an optional field). Otherwise a layer above it would hide
  it, or the merge would replace it unread.
* A form's table is the form: validating the table gives what validating the form gives.
"""

import re
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Callable, ClassVar, Dict


from xeda import Design
from xeda.dataclass import (
    WHOLE,
    Field,
    annotation_form,
    annotation_kind,
    annotation_mistake,
    appended_fields_of,
    field_shorthands_of,
    input_names,
    is_mistake,
    mapping_form,
    nested_model,
    shape_problems,
    table_entries,
)
from xeda.design import DesignValidationError, RtlSettings
from xeda.flow import FlowSettingsError
from xeda.flow.fpga import FPGA
from xeda.flow.synth import SynthFlow
from xeda.flow_runner.settings_layers import merge_layers
from xeda.flow_runner.resolver import INDIVISIBLE_SETTINGS
from xeda.flows import VivadoSynth, Yosys

from .settings_samples import flow_classes, minimal_settings
from .test_target_shapes import PROBE_VALUES

#: Values a field of a real design takes that the structural probes do not hold.
NAMED = ["asap7", "xc7a35tcpg236-1", "08", 2008]
VALUES = [*PROBE_VALUES, *NAMED]

#: Where the designs and settings of the sweep are rooted. Nothing is written there.
_ROOT = Path(__file__).parent


def table_fields(root: type) -> list[tuple[type, str]]:
    """Every field that takes a table, with the model that holds it, through the nested models
    reachable from `root`."""
    found: list[tuple[type, str]] = []
    seen: set[type] = set()

    def walk(model: type) -> None:
        if model in seen:
            return
        seen.add(model)
        for name, info in model.model_fields.items():
            kind, child, _ = annotation_kind(info.annotation)
            if kind in ("model", "dict"):
                found.append((model, name))
            # a platform is a whole model read from a file, never merged key by key
            if child is not None and name not in INDIVISIBLE_SETTINGS:
                walk(child)

    walk(root)
    return found


def _heads(errors: list) -> set[str]:
    heads = set()
    for error in errors:
        location = error["loc"] if isinstance(error, dict) else error[0]
        if isinstance(location, (tuple, list)):
            heads.add(str(location[0]) if location else "")
        else:
            heads.add(re.split(r"\s*(?:->|\.)\s*", location or "")[0])
    return heads


def flow_of_settings() -> dict[type, type]:
    return {flow_class.Settings: flow_class for flow_class, _ in flow_classes()}


def validated(owner: type, name: str, value: Any) -> Any:
    """What `owner` makes of `{name: value}`: the validated fields as plain data, or the set of
    fields that were refused. Only the field is judged: the others may be missing."""
    spellings = {key for key, field in input_names(owner).items() if field == name}
    if issubclass(owner, Design):
        data: dict = {"name": "d", "rtl": {"sources": [], "top": "t"}, name: value}
        try:
            return ("valid", Design(design_root=_ROOT, **data).model_dump(mode="json"))
        except DesignValidationError as e:
            return ("refused", _heads(e.errors) & spellings or {name})
    if owner in flow_of_settings():  # a flow's settings
        try:
            settings = owner.from_input(
                {**minimal_settings(flow_of_settings()[owner], clock_period=False), name: value},
                design_root=_ROOT,
                runner_cwd=_ROOT,
            )
            return ("valid", settings.model_dump(mode="json"))
        except FlowSettingsError as e:
            heads = _heads(e.errors)
            return ("refused", heads & spellings) if heads & spellings else ("valid", None)
    try:
        return ("valid", owner.model_validate({name: value}).model_dump(mode="json"))
    except Exception as e:  # noqa: BLE001 - pydantic's error, or a validator's
        errors = e.errors() if hasattr(e, "errors") else [{"loc": (name,)}]
        heads = _heads(errors)
        return ("refused", heads & spellings) if heads & spellings else ("valid", None)


def fields_of_the_design_and_the_flows() -> list[tuple[type, str, str]]:
    """`(model, field, where)` for every table-taking field of the design and of each flow."""
    fields: list[tuple[type, str, str]] = [
        (model, name, "design") for model, name in table_fields(Design)
    ]
    for flow_class, flow_name in flow_classes():
        for model, name in table_fields(flow_class.Settings):
            fields.append((model, name, flow_name if model is flow_class.Settings else "nested"))
    unique = {}
    for model, name, where in fields:
        unique.setdefault((model, name), where)
    return [(model, name, where) for (model, name), where in unique.items()]


def problems_of_the_forms() -> list[str]:
    """What the sweep finds wrong: a field that accepts a value its merge cannot read."""
    problems = []
    for model, name, where in fields_of_the_design_and_the_flows():
        for value in VALUES:
            if isinstance(value, Mapping):
                continue
            try:
                outcome = validated(model, name, value)
            except Exception as e:  # noqa: BLE001 - reported as a finding, not a crash
                problems.append(f"{model.__qualname__}.{name} = {value!r} ({where}): {e!r:.100}")
                continue
            if outcome[0] == "valid" and is_mistake(model, name, value):
                problems.append(
                    f"{model.__qualname__}.{name} accepts {value!r} ({where}), which the merge "
                    "reads as a mistake: add its `as_mapping` or `field_shorthands` entry"
                )
                continue
            form = mapping_form(model, name, value)
            if isinstance(form, Mapping):
                again = validated(model, name, dict(form))
                if again != outcome:
                    problems.append(
                        f"{model.__qualname__}.{name} = {value!r} ({where}): its table {form!r} "
                        f"is validated as {again!r}, the form as {outcome!r}"
                    )
    return problems


def test_every_form_a_field_accepts_stands_for_a_table_and_validates_as_it():
    assert problems_of_the_forms() == []


def test_the_sweep_finds_a_form_that_has_no_table(monkeypatch):
    """The mutation check: when the merge cannot read the part number as a table, the sweep
    names the field that still accepts it."""
    import xeda.dataclass as dataclass_module

    read = dataclass_module.mapping_form

    def forgetting_the_part_number(owner, field, value):
        return (
            value
            if isinstance(value, Mapping)
            else None if field == "fpga" else read(owner, field, value)
        )

    monkeypatch.setattr(dataclass_module, "mapping_form", forgetting_the_part_number)
    monkeypatch.setattr(sys.modules[__name__], "mapping_form", forgetting_the_part_number)
    assert any("fpga" in problem and "accepts" in problem for problem in problems_of_the_forms())


def test_the_sweep_finds_a_field_shorthand_that_was_dropped(monkeypatch):
    from xeda.design import DVSettings

    monkeypatch.setattr(DVSettings, "field_shorthands", {})
    assert any("parameters" in problem for problem in problems_of_the_forms())


def test_the_sweep_walks_the_fields_that_have_forms():
    """Not an empty walk: the fields that are known to take a form are among those it visits."""
    visited = {
        (model.__qualname__, name) for model, name, _ in fields_of_the_design_and_the_flows()
    }
    for expected in [
        ("Settings", "fpga"),
        ("Design", "rtl"),
        ("Design", "language"),
        ("Language", "vhdl"),
        ("TbSettings", "cocotb"),
        ("RtlSettings", "parameters"),
        ("TbSettings", "parameters"),
        ("RtlSettings", "attributes"),
    ]:
        assert any(
            qualname.endswith(expected[0]) and name == expected[1] for qualname, name in visited
        ), expected


def dictionaries_of_tables(root: type) -> list[tuple[type, str, Any]]:
    """`(model, field, annotation of the entries)` for every field of `root` and of the models
    reachable from it that holds a dictionary whose entries are tables (`rtl.attributes`, a
    flow's `clocks`, a platform's `corner`)."""
    found: list[tuple[type, str, Any]] = []
    seen: set[type] = set()

    def walk(model: type) -> None:
        if model in seen:
            return
        seen.add(model)
        for name, info in model.model_fields.items():
            entries = table_entries(info.annotation)
            if entries is not None:
                found.append((model, name, entries))
            child = nested_model(info.annotation)
            if child is not None:
                walk(child)
            if entries is not None and nested_model(entries) is not None:
                walk(nested_model(entries))

    walk(root)
    return found


def every_dictionary_of_tables() -> list[tuple[type, str, Any]]:
    found = dictionaries_of_tables(Design)
    for flow_class, _name in flow_classes():
        found += dictionaries_of_tables(flow_class.Settings)
    unique = {(model, name): (model, name, entries) for model, name, entries in found}
    return list(unique.values())


def problems_of_the_entries() -> list[str]:
    """What the sweep finds wrong: a dictionary of tables whose entry that is no table is hidden by
    a table written over it in a higher layer, or goes unreported by the shape check."""
    problems = []
    for model, name, entries in every_dictionary_of_tables():
        where = f"{model.__qualname__}.{name}"
        for probe in PROBE_VALUES:
            if not annotation_mistake(entries, probe, annotation_form(entries, probe)):
                continue
            below = {name: {"e": probe}}
            merged = merge_layers(below, {name: {"e": {}}}, settings_cls=model)
            if merged != below:
                problems.append(f"{where}: a table over {probe!r} gives {merged!r}")
            if shape_problems(model, below) != [((name, "e"), probe)]:
                problems.append(f"{where}: {probe!r} as an entry is not reported by its shape")
        well_formed = merge_layers({name: {"e": {}}}, {name: {"e": {}}}, settings_cls=model)
        if well_formed != {name: {"e": {}}}:
            problems.append(f"{where}: two empty tables give {well_formed!r}")
    return problems


def test_an_entry_that_is_no_table_is_kept_whatever_table_is_written_over_it():
    assert problems_of_the_entries() == []


def test_the_sweep_finds_a_table_that_replaces_a_malformed_entry(monkeypatch):
    """The mutation check: when the merge does not know the entries are tables, the sweep names
    the dictionaries."""
    import xeda.flow_runner.settings_layers as layers

    monkeypatch.setattr(layers, "table_entries", lambda annotation: None)
    found = problems_of_the_entries()
    assert any("RtlSettings.attributes" in problem for problem in found), found


def test_the_sweep_walks_the_dictionaries_of_tables():
    visited = {(model.__qualname__, name) for model, name, _ in every_dictionary_of_tables()}
    for expected in [
        ("RtlSettings", "attributes"),
        ("Settings", "clocks"),
        ("Settings", "set_mod_attribute"),
        ("AsicsPlatform", "corner"),
    ]:
        assert any(
            qualname.endswith(expected[0]) and name == expected[1] for qualname, name in visited
        ), expected
    assert ("RtlSettings", "parameters") not in visited, "its entries are values, not tables"


def test_the_forms_that_stand_for_a_table():
    """The forms themselves, one by one."""
    assert FPGA.as_mapping("xc7a35tcpg236-1") == {"part": "xc7a35tcpg236-1"}
    assert FPGA.as_mapping(5) is None
    from xeda.design import Clock, CocotbTestbench, LanguageSettings, VhdlSettings

    assert Clock.as_mapping("CLK") == {"port": "CLK"}
    assert Clock.as_mapping(3) is None
    assert CocotbTestbench.as_mapping(True) == {}
    assert CocotbTestbench.as_mapping(False) is WHOLE
    assert CocotbTestbench.as_mapping("x") is None
    assert VhdlSettings.as_mapping("08") == {"standard": "08"}
    assert LanguageSettings.as_mapping(2005) == {"standard": "2005"}
    assert LanguageSettings.as_mapping(True) is None  # a bool is no standard
    from xeda.platforms.asics import AsicsPlatform

    assert AsicsPlatform.as_mapping("asap7") is WHOLE


def test_a_model_already_built_is_a_whole_value():
    assert mapping_form(VivadoSynth.Settings, "fpga", FPGA(part="xc7a35tcpg236-1")) is WHOLE
    assert nested_model(VivadoSynth.Settings.model_fields["fpga"].annotation) is FPGA
    assert mapping_form(Yosys.Settings, "platform", "asap7") is WHOLE


def test_no_module_converts_a_part_number_or_a_port_by_itself():
    """A form is read by its model's `as_mapping`, never by a second copy of the conversion:
    the table of a part number is spelled in `flow/fpga.py`, and that of a port in `design.py`."""
    source = Path(__file__).parent.parent / "src" / "xeda"
    spelled = [
        f"{path.relative_to(source)}:{number}"
        for path in sorted(source.rglob("*.py"))
        for number, line in enumerate(path.read_text().splitlines(), 1)
        if re.search(r"""\{\s*["']part["']\s*:\s*\w+\s*\}""", line)
        and path.name not in ("fpga.py",)
        or re.search(r"""\{\s*["']port["']\s*:\s*\w+\s*\}""", line)
        and path.name != "design.py"
    ]
    assert not spelled, f"use FPGA.as_mapping or Clock.as_mapping: {spelled}"


class _SettingsWithAForm(SynthFlow.Settings):
    """A flow's settings that declare a form of their own and repeat none of their base's."""

    table: Dict[str, Any] = Field({}, description="a table that takes a text as its one key")
    field_shorthands: ClassVar[Mapping[str, Callable[[Any], Any]]] = {
        "table": lambda value: {"text": value} if isinstance(value, str) else None
    }


class _SettingsWithAnotherForm(_SettingsWithAForm):
    field_shorthands: ClassVar[Mapping[str, Callable[[Any], Any]]] = {
        "clocks": lambda value: {} if value is None else None
    }


class _Rtl(RtlSettings):
    appended_fields: ClassVar[tuple[str, ...]] = ("top",)


def test_a_subclass_adds_its_forms_to_those_of_its_bases():
    """`SynthFlow` declares the form of `clocks`: a subclass that declares another keeps it."""
    assert mapping_form(_SettingsWithAForm, "clocks", None) is WHOLE
    assert mapping_form(_SettingsWithAForm, "table", "x") == {"text": "x"}
    assert set(field_shorthands_of(_SettingsWithAForm)) == {"clocks", "table"}


def test_the_form_a_subclass_declares_for_a_field_wins_over_its_base():
    assert mapping_form(_SettingsWithAnotherForm, "clocks", None) == {}
    assert mapping_form(_SettingsWithAnotherForm, "table", "x") == {"text": "x"}


def test_a_subclass_adds_its_appended_fields_to_those_of_its_bases():
    assert appended_fields_of(RtlSettings) == ("sources",)
    assert appended_fields_of(_Rtl) == ("sources", "top")
    below, above = {"sources": ["a"], "top": ["x"]}, {"sources": ["b"], "top": ["y"]}
    appended = merge_layers(below, above, settings_cls=_Rtl, append=True)
    assert appended == {"sources": ["a", "b"], "top": ["x", "y"]}


def test_a_layer_replaces_an_appended_field_unless_the_merge_appends():
    """Only a target adds to the design's `sources`; a list of any other layer replaces."""
    below, above = {"sources": ["a"], "top": "x"}, {"sources": ["b"], "top": "y"}
    assert merge_layers(below, above, settings_cls=RtlSettings) == above
    assert merge_layers(below, above, settings_cls=RtlSettings, append=True) == {
        "sources": ["a", "b"],
        "top": "y",
    }
