"""Every name has one spelling: a setting, a design key, a flow and a command-line option.

A second spelling of a name that stays is refused, and the error names the spelling to use. The
table `REMOVED_SPELLINGS` lists each one. `test_the_table_covers_every_second_spelling_in_the_code`
finds them in the models, so a new second spelling without a row fails.
"""

import json
from collections.abc import Iterator
from typing import Any, get_args

import click
import pytest
from pydantic import BaseModel

import xeda.flows  # noqa: F401  (registers the built-in flows)
from xeda.cli_types import ChainChoice, FlowChoice
from xeda.dataclass import SecondSpelling, XedaBaseModel, field_type
from xeda.design import (
    Clock,
    Design,
    DesignValidationError,
    Generator,
    Language,
    LanguageSettings,
    Rtl,
    VhdlSettings,
)
from xeda import design as design_module
from xeda.flow import FlowSettingsError, registered_flows
from xeda.flow_runner import FlowNotFoundError, get_flow_class
from xeda.flow_runner.chains import parse_request
from xeda.flow_runner.error_codes import public_error, settings_codes
from xeda.flow_runner.settings_layers import merge_flow_sections
from xeda.xedaproject import XedaProject

#: The flows of Xeda. A test module may register a flow of its own, named with a leading `_`.
FLOWS = sorted(
    {cls for _, cls in registered_flows.values() if not cls.__name__.startswith("_")},
    key=lambda cls: cls.name,
)

# ------------------------------------------------------------------------------ no field alias


def _reachable(model: type[BaseModel], found: dict[type, None]) -> None:
    """Add `model` and the models that its fields hold, at any depth, to `found`."""
    if model in found:
        return
    found[model] = None

    def visit(annotation: Any) -> None:
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            _reachable(annotation, found)
        for argument in get_args(annotation):
            visit(argument)
        for member in getattr(annotation, "models", ()):
            visit(member)

    for info in model.model_fields.values():
        visit(field_type(info))


def _all_models() -> list[type[BaseModel]]:
    found: dict[type, None] = {}
    for flow in FLOWS:
        _reachable(flow.Settings, found)
    _reachable(Design, found)
    for model in (Generator, Language):
        _reachable(model, found)
    return list(found)


ALL_MODELS = _all_models()


def test_the_models_to_check_include_the_nested_ones():
    names = {model.__name__ for model in ALL_MODELS}

    assert {"Design", "Rtl", "Testbench", "Language", "VhdlSettings", "Generator", "Clock"} <= names
    assert {"FPGA", "PhysicalClock"} <= names


@pytest.mark.parametrize("model", ALL_MODELS, ids=lambda model: model.__qualname__)
def test_no_field_has_an_alias(model):
    for name, info in model.model_fields.items():
        assert info.alias is None, f"{model.__qualname__}.{name}"
        assert info.validation_alias is None, f"{model.__qualname__}.{name}"
        assert info.serialization_alias is None, f"{model.__qualname__}.{name}"


def test_no_flow_has_a_second_name():
    assert not any(hasattr(flow, "aliases") for flow in FLOWS)
    assert all(cls.name == key for key, (_, cls) in registered_flows.items())


# ------------------------------------------------------------------------------ the table

#: One row for each second spelling that Xeda refused after it accepted it:
#: (model, the spelling as a dotted path, the input that gives it, the code of the error, the text
#: after "use " in the message).
#: `model` is a flow, `design` (a design file) or `project` (a project file).
REMOVED_SPELLINGS = [
    # --- the settings of a flow
    ("yosys_fpga", "clock", {"clock": {"period": "10 ns"}}, "removed_alias", "`clocks.<name>`"),
    ("nextpnr", "ncpus", {"ncpus": 4}, "removed_alias", "`nthreads`"),
    ("vivado_synth", "fpga", {"fpga": "xc7a35t"}, "removed_alias", "`fpga.part`"),
    ("yosys", "library", {"library": ["a.lib"]}, "removed_alias", "`liberty`"),
    ("ghdl_sim", "warn_error", {"warn_error": True}, "removed_alias", "`werror`"),
    ("ghdl_sim", "elab_warn_error", {"elab_warn_error": True}, "removed_alias", "`elab_werror`"),
    ("nvc", "warn_error", {"warn_error": True}, "removed_alias", "`werror`"),
    ("yosys_fpga", "netlist", {"netlist": "a.v"}, "removed_alias", "`netlist_verilog`"),
    ("yosys_fpga", "json_netlist", {"json_netlist": "a.json"}, "removed_alias", "`netlist_json`"),
    # --- the keys of a design file
    (
        "design",
        "rtl.generics",
        {"rtl": {"generics": {"W": 8}}},
        "removed_alias",
        "`rtl.parameters`",
    ),
    (
        "design",
        "tb.generics",
        {"tb": {"generics": {"W": 8}}},
        "removed_alias",
        "`tb.parameters`",
    ),
    (
        "design",
        "rtl.clock",
        {"rtl": {"clock": {"port": "clk"}}},
        "removed_alias",
        "`rtl.clocks`",
    ),
    (
        "design",
        "language.vhdl.version",
        {"language": {"vhdl": {"version": "08"}}},
        "removed_alias",
        "`language.vhdl.standard`",
    ),
    (
        "design",
        "language.verilog.version",
        {"language": {"verilog": {"version": "2005"}}},
        "removed_alias",
        "`language.verilog.standard`",
    ),
    (
        "design",
        "rtl.generator.use",
        {"rtl": {"generator": {"use": "chisel"}}},
        "removed_alias",
        "`class`",
    ),
    ("design", "author", {"author": "a"}, "removed_alias", "`authors`"),
    ("design", "hdl", {"hdl": {}}, "removed_alias", "`language`"),
    ("design", "tests", {"tests": {}}, "removed_alias", "`tb`"),
    ("design", "test", {"test": {}}, "removed_alias", "`tb`"),
    ("design", "flow", {"flow": {}}, "removed_alias", "`flows`"),
    # --- the flat keys at the root of a design file
    ("design", "sources", {"sources": ["a.v"]}, "removed_alias", "`rtl.sources`"),
    ("design", "top", {"top": "a"}, "removed_alias", "`rtl.top`"),
    ("design", "clock", {"clock": {"port": "clk"}}, "removed_alias", "`rtl.clocks`"),
    ("design", "clocks", {"clocks": [{"port": "clk"}]}, "removed_alias", "`rtl.clocks`"),
    ("design", "parameters", {"parameters": {"W": 8}}, "removed_alias", "`rtl.parameters`"),
    ("design", "generics", {"generics": {"W": 8}}, "removed_alias", "`rtl.parameters`"),
    ("design", "defines", {"defines": {"X": 1}}, "removed_alias", "`rtl.defines`"),
    (
        "design",
        "generator",
        {"generator": {"command": "true"}},
        "removed_alias",
        "`rtl.generator`",
    ),
    # --- a key that is gone, with the key that replaces it
    ("design", "clock_port", {"clock_port": "clk"}, "removed_key", "`rtl.clocks`"),
    # --- the keys of a project file
    ("project", "design", {"design": {"name": "d"}}, "removed_alias", "`designs`"),
    ("project", "flow", {"flow": {}}, "removed_alias", "`flows`"),
]

#: The same rows as a literal set, so that dropping a row, or adding one for a spelling that
#: never existed, fails here and not silently.
SPELLINGS_OF_TODAY = {
    "yosys_fpga:clock",
    "nextpnr:ncpus",
    "vivado_synth:fpga",
    "yosys:library",
    "ghdl_sim:warn_error",
    "ghdl_sim:elab_warn_error",
    "nvc:warn_error",
    "yosys_fpga:netlist",
    "yosys_fpga:json_netlist",
    "design:rtl.generics",
    "design:tb.generics",
    "design:rtl.clock",
    "design:language.vhdl.version",
    "design:language.verilog.version",
    "design:rtl.generator.use",
    "design:author",
    "design:hdl",
    "design:tests",
    "design:test",
    "design:flow",
    "design:sources",
    "design:top",
    "design:clock",
    "design:clocks",
    "design:parameters",
    "design:generics",
    "design:defines",
    "design:generator",
    "design:clock_port",
    "project:design",
    "project:flow",
}


def test_the_table_lists_each_second_spelling_once():
    rows = {f"{model}:{spelling}" for model, spelling, *_ in REMOVED_SPELLINGS}

    assert len(rows) == len(REMOVED_SPELLINGS)
    assert rows == SPELLINGS_OF_TODAY


def _design_models() -> Iterator[tuple[str, type[XedaBaseModel]]]:
    """Every model of a design file that can refuse a key, with the path of the table."""
    yield "", Design
    yield "rtl", Rtl
    yield "tb", design_module.Testbench
    yield "language.vhdl", VhdlSettings
    yield "language.verilog", LanguageSettings
    yield "rtl.generator", Generator
    yield "rtl.clocks", Clock


def test_the_table_covers_every_second_spelling_in_the_code():
    """A second spelling that a flow or the design models refuse has a row."""
    in_table = {(model, spelling) for model, spelling, *_ in REMOVED_SPELLINGS}
    of_flows = {spelling for model, spelling in in_table if model in {cls.name for cls in FLOWS}}
    for flow in FLOWS:
        for name, replacement in flow.Settings.removed_setting_replacements().items():
            if isinstance(replacement, SecondSpelling):
                assert name in of_flows, f"{flow.name}: `{name}` has no row"
    for path, model in _design_models():
        for name, replacement in model.removed_inputs.items():
            if isinstance(replacement, SecondSpelling):
                dotted = f"{path}.{name}" if path else name
                assert ("design", dotted) in in_table, f"design: `{dotted}` has no row"


@pytest.mark.parametrize(
    ("flow", "spelling", "given", "code", "successor"),
    [row for row in REMOVED_SPELLINGS if row[0] in {cls.name for cls in FLOWS}],
    ids=lambda value: value if isinstance(value, str) and len(value) < 24 else "",
)
def test_a_removed_setting_names_its_successor(flow, spelling, given, code, successor):
    settings = registered_flows[flow][1].Settings

    with pytest.raises(FlowSettingsError) as refused:
        settings.from_input(given)

    assert settings_codes(refused.value)[0] == code
    assert f"was removed: use {successor}" in str(refused.value)
    assert f"was removed: use {successor}" in public_error(refused.value).message


def _design_document(given: dict[str, Any]) -> dict[str, Any]:
    document: dict[str, Any] = {"name": "d", "rtl": {"sources": []}}
    for key, value in given.items():
        if isinstance(value, dict) and isinstance(document.get(key), dict):
            document[key] = {**document[key], **value}
        else:
            document[key] = value
    return document


@pytest.mark.parametrize(
    ("spelling", "given", "code", "successor"),
    [row[1:] for row in REMOVED_SPELLINGS if row[0] == "design"],
    ids=[row[1] for row in REMOVED_SPELLINGS if row[0] == "design"],
)
def test_a_removed_design_key_names_its_successor(tmp_path, spelling, given, code, successor):
    file = tmp_path / "d.xeda.json"
    file.write_text(json.dumps(_design_document(given)))

    with pytest.raises(DesignValidationError) as refused:
        Design.from_file(file)

    error = public_error(refused.value)
    assert error.code == code
    assert f"`{spelling.rpartition('.')[2]}` was removed: use {successor}" in error.message


@pytest.mark.parametrize("part", ["rtl", "tb"])
def test_a_removed_key_in_a_target_is_refused_where_it_is_written(tmp_path, part):
    file = tmp_path / "d.xeda.json"
    document = _design_document({"targets": {"board": {part: {"generics": {"W": 8}}}}})
    file.write_text(json.dumps(document))

    with pytest.raises(DesignValidationError) as refused:
        Design.from_file(file)

    location, message, _, kind = refused.value.errors[0]
    assert location == f"targets.board.{part}.generics"
    assert kind == "removed_alias"
    assert f"use `{part}.parameters`" in message


def test_a_flat_key_in_a_target_is_refused_with_the_key_to_use(tmp_path):
    file = tmp_path / "d.xeda.json"
    file.write_text(json.dumps(_design_document({"targets": {"board": {"sources": ["a.v"]}}})))

    with pytest.raises(DesignValidationError) as refused:
        Design.from_file(file)

    assert "`sources` was removed: use `rtl.sources`" in refused.value.errors[0][1]


def test_a_flat_key_in_the_design_overrides_is_refused_with_the_key_to_use(tmp_path):
    file = tmp_path / "d.xeda.json"
    file.write_text(json.dumps(_design_document({})))

    with pytest.raises(DesignValidationError) as refused:
        Design.from_file(file, overrides={"top": "a"})

    assert "`top` was removed: use `rtl.top`" in refused.value.errors[0][1]


@pytest.mark.parametrize(
    ("spelling", "given", "code", "successor"),
    [row[1:] for row in REMOVED_SPELLINGS if row[0] == "project"],
    ids=[row[1] for row in REMOVED_SPELLINGS if row[0] == "project"],
)
def test_a_removed_project_key_names_its_successor(tmp_path, spelling, given, code, successor):
    file = tmp_path / "xedaproject.json"
    file.write_text(json.dumps(given))

    with pytest.raises(ValueError, match=f"`{spelling}` was removed: use {successor}"):
        XedaProject.from_file(file)


# ------------------------------------------------------------------------------ flow names

#: A spelling of a flow other than its name, and the name to use.
REMOVED_FLOW_NAMES = [
    ("ghdl", "ghdl_sim"),
    ("ghdl-sim", "ghdl_sim"),
    ("vivado-synth", "vivado_synth"),
    ("VivadoSynth", "vivado_synth"),
    ("Nextpnr", "nextpnr"),
    ("NEXTPNR", "nextpnr"),
    ("GhdlSim", "ghdl_sim"),
]


@pytest.mark.parametrize(("given", "canonical"), REMOVED_FLOW_NAMES)
def test_a_removed_flow_name_names_its_successor(given, canonical):
    with pytest.raises(FlowNotFoundError) as refused:
        get_flow_class(given)
    assert refused.value.code == "flow_not_found"
    assert refused.value.suggestions[0] == canonical

    with pytest.raises(click.BadParameter, match=f"Did you mean: {canonical}"):
        FlowChoice().convert(given, None, None)
    with pytest.raises(click.BadParameter, match=f"Did you mean: {canonical}"):
        ChainChoice().convert(f"{given}+{canonical}", None, None)

    with pytest.raises(FlowNotFoundError) as in_chain:
        parse_request(given)
    assert in_chain.value.suggestions[0] == canonical

    with pytest.raises(FlowNotFoundError) as in_section:
        merge_flow_sections({given: {}})
    assert in_section.value.suggestions[0] == canonical


def test_the_name_of_a_flow_is_the_only_spelling_the_cli_completes():
    from xeda.flow_catalog import flow_names

    assert {cls.name for cls in FLOWS} <= set(flow_names())
    assert all(name == name.lower() and "-" not in name for name in flow_names())


def test_the_flow_listing_has_no_aliases():
    from xeda.introspect import flows_info

    assert all("aliases" not in flow for flow in flows_info())
