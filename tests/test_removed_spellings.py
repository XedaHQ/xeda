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
from xeda.xedaproject import ProjectFileError, XedaProject

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

    with pytest.raises(ProjectFileError, match=f"`{spelling}` was removed: use {successor}") as bad:
        XedaProject.from_file(file)

    error = public_error(bad.value)
    assert (type(error).__name__, error.code) == ("ProjectError", code)


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


# ------------------------------------------------------------------------------ options

#: A removed option, the arguments to give it with, and the text after "use " in the message.
REMOVED_OPTIONS = [
    ("--flow-settings", ["run", "vivado_synth", "d.yaml"], "-s or --settings"),
    ("--flow-settings", ["dse", "vivado_synth"], "-s or --settings"),
    ("--design-file", ["run", "vivado_synth"], "the positional DESIGN argument"),
    ("--design", ["run", "vivado_synth"], "the positional DESIGN argument"),
    ("--design-file", ["dse", "vivado_synth"], "the positional DESIGN argument"),
    ("--design", ["dse", "vivado_synth"], "the positional DESIGN argument"),
    ("--help-settings", ["run", "vivado_synth", "d.yaml"], "`xeda list-settings FLOW`"),
    ("--list-settings", ["run", "vivado_synth", "d.yaml"], "`xeda list-settings FLOW`"),
    ("--debug", [], "--log-level 10 and --detailed-logs"),
    ("-d", [], "--log-level 10 and --detailed-logs"),
    ("--debug", ["run", "vivado_synth", "d.yaml"], "--log-level 10 and --detailed-logs"),
    ("-d", ["run", "vivado_synth", "d.yaml"], "--log-level 10 and --detailed-logs"),
    ("--debug", ["dse", "vivado_synth"], "--log-level 10 and --detailed-logs"),
    ("-v", [], "--log-level 10 for more log messages"),
    ("--verbose", [], "--log-level 10 for more log messages"),
    ("-q", [], "--log-level 30 for fewer log messages"),
    ("--quiet", [], "--log-level 30 for fewer log messages"),
    ("--init_freq_low", ["dse", "vivado_synth"], "--init-freq-low"),
    ("--init_freq_high", ["dse", "vivado_synth"], "--init-freq-high"),
]


@pytest.mark.parametrize(
    ("option", "command", "successor"),
    REMOVED_OPTIONS,
    ids=[f"{' '.join(command[:1])} {option}" for option, command, _ in REMOVED_OPTIONS],
)
def test_a_removed_option_names_its_successor(tmp_path, option, command, successor):
    from click.testing import CliRunner

    from xeda.cli import cli

    design = tmp_path / "d.yaml"
    design.write_text("name: d\n")
    # The group options come first; the options of a command follow its arguments.
    args = [option, "list-flows"] if not command else [*command, option]
    args = [str(design) if arg == "d.yaml" else arg for arg in args]
    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 2, result.output
    assert f"`{option}` was removed: use {successor}" in click.unstyle(result.output)


def test_the_removed_debug_variable_names_itself(monkeypatch):
    from click.testing import CliRunner

    from xeda.cli import cli

    result = CliRunner().invoke(cli, ["list-flows"], env={"XEDA_DEBUG": "1"})

    assert result.exit_code == 2, result.output
    assert "`XEDA_DEBUG` was removed: use --log-level 10 and --detailed-logs" in click.unstyle(
        result.output
    )


def test_the_commands_take_the_design_as_an_argument_and_the_settings_as_dash_s():
    from xeda.cli import dse, run

    for command in (run, dse):
        visible = {
            name: param
            for param in command.params
            if not getattr(param, "hidden", False)
            for name in getattr(param, "opts", [])
        }
        assert "-s" in visible and "--settings" in visible
        assert not {"--flow-settings", "--design", "--design-file", "--debug"} & set(visible)
        arguments = [param.name for param in command.params if param.param_type_name == "argument"]
        assert arguments[-1] == ("design_file" if command is run else "design")
    assert [p.name for p in dse.params if p.param_type_name == "argument"] == ["flow", "design"]
    assert "--init-freq-low" in {opt for p in dse.params for opt in p.opts}


@pytest.mark.parametrize("command", [["run", "vivado_synth", "d.yaml"], ["dse", "vivado_synth"]])
@pytest.mark.parametrize("option", ["--design-allow-extra", "--no-design-allow-extra"])
def test_the_option_that_allowed_extra_design_keys_is_gone(tmp_path, command, option):
    from click.testing import CliRunner

    from xeda.cli import cli

    design = tmp_path / "d.yaml"
    design.write_text("name: d\n")
    args = [str(design) if arg == "d.yaml" else arg for arg in [*command, option]]
    result = CliRunner().invoke(cli, args)

    assert result.exit_code == 2, result.output
    assert (
        f"`{option}` was removed: a design file holds the keys of the design and nothing else; "
        "delete it"
    ) in click.unstyle(result.output)


# ------------------------------------------------------------------------------ letter case

from xeda import units  # noqa: E402


def _quantity_classes():
    return [cls for cls in units.Quantity.__subclasses__() if getattr(cls, "units", None)]


UNIT_SYMBOLS = [(cls, symbol) for cls in _quantity_classes() for symbol in cls.units]


def test_every_kind_of_quantity_is_covered_by_the_letter_case_rows():
    assert {cls.__name__ for cls in _quantity_classes()} >= {
        "Time",
        "Frequency",
        "Capacitance",
        "Length",
        "Voltage",
        "Resistance",
        "Power",
    }


@pytest.mark.parametrize(
    ("cls", "symbol"), UNIT_SYMBOLS, ids=[f"{c.__name__}-{s}" for c, s in UNIT_SYMBOLS]
)
def test_a_unit_symbol_is_accepted_only_in_its_own_letter_case(cls, symbol):
    assert cls(f"3 {symbol}") is not None  # the spelling of the docs
    for other in {symbol.upper(), symbol.lower(), symbol.swapcase(), symbol.capitalize()}:
        if other in cls.units:  # another symbol of the same kind (`mV` and `V`)
            continue
        with pytest.raises(ValueError, match="case-sensitive") as refused:
            cls(f"3 {other}")
        assert any(f"`{known}`" in str(refused.value) for known in cls.units), other


@pytest.mark.parametrize(
    ("wrong", "right"), [("MV", "mV"), ("Ms", "ms"), ("mhz", "MHz"), ("PF", "pF"), ("mw", "mW")]
)
def test_a_unit_that_si_reads_as_another_unit_is_refused_and_the_right_one_named(wrong, right):
    kind = next(cls for cls in _quantity_classes() if right in cls.units)
    with pytest.raises(ValueError, match=f"did you mean `{right}`"):
        kind(f"5 {wrong}")
