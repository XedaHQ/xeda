"""A malformed design or settings file must be reported as a validation error, never a traceback.

pydantic v1 only ever handed a `pre=True` root validator a mapping; v2 hands a `mode="before"`
model validator whatever the input was, so every v1-era body written against `values.get(...)`
crashed with `AttributeError` on, say, `fpga = ["x"]`. Code that runs *before* validation
(`Design.__init__`, platform lookup, file resolution) could leak `FileNotFoundError`,
`AssertionError` and `KeyError` the same way. On the command line each of those surfaced as a
traceback, and under `--json` with the wrong `error.type`.

The sweep below feeds a spread of wrong-typed values into every field of every flow's settings and
of a design, so a new field or validator cannot reintroduce the leak unnoticed.
"""

import json
from pathlib import Path
from typing import Any

import click
import pytest
from click.testing import CliRunner

from xeda.cli import cli
from xeda.cli_utils import OptionEatAll
from xeda.dataclass import ValidationError
from xeda.design import Design, DesignValidationError
from xeda.flow import FPGA, FlowSettingsError
from xeda.flow_runner import DefaultRunner
from xeda.flows.yosys.yosys_fpga import YosysFpga
from xeda.platforms.asics import AsicsPlatform
from xeda.utils import XedaException

from .project_files import PROJECT_FILE
from .settings_samples import PROBES, flow_classes, minimal_settings

#: What a malformed value may raise: the validation errors of settings (`FlowSettingsError`),
#: designs (`DesignValidationError`) and plain models (pydantic's `ValidationError`, the only one
#: of the three that is a `ValueError`), plus the `ValueError` that lookups such as
#: `AsicsPlatform.from_resource` raise for a name they do not know.
VALIDATION_ERRORS = (ValidationError, FlowSettingsError, DesignValidationError, ValueError)

#: Every structural kind of value, plus a few that only a caller (not a file) can pass.
MALFORMED = [*PROBES, object(), {"a": {"b": 1}}, [{}]]

RTL = {"sources": [], "top": "t"}

FLOWS = flow_classes()


def _leaks(build, fields):
    """Every (field, value) whose construction raised something other than a validation error."""
    leaks = []
    for field in fields:
        for bad in MALFORMED:
            try:
                build(field, bad)
            except VALIDATION_ERRORS:
                pass
            except Exception as e:  # reporting exactly these is the point
                leaks.append(f"{field}={bad!r}: {type(e).__name__}: {e}")
    return leaks


@pytest.mark.parametrize("cls", [cls for cls, _ in FLOWS], ids=[name for _, name in FLOWS])
def test_malformed_flow_settings_are_validation_errors(cls):
    base = minimal_settings(cls)
    fields = [name for name in cls.Settings.model_fields if not name.endswith("_")]

    leaks = _leaks(lambda field, bad: cls.Settings(**{**base, field: bad}), fields)

    assert not leaks, f"{cls.name}:\n  " + "\n  ".join(leaks)


@pytest.mark.parametrize("section", ["rtl", "tb"])
def test_malformed_design_sections_are_validation_errors(section):
    fields = ["sources", "top", "parameters", "generics", "defines", "clock", "clocks", "cocotb"]

    def build(field, bad):
        payload = {"name": "d", "rtl": dict(RTL)}
        payload.setdefault(section, {})[field] = bad
        Design(**payload)

    leaks = _leaks(build, fields)

    assert not leaks, f"{section}:\n  " + "\n  ".join(leaks)


def test_malformed_top_level_design_keys_are_validation_errors():
    fields = ["name", "flow", "dependencies", "authors", "language", "rtl", "tb"]

    leaks = _leaks(lambda field, bad: Design(**{"name": "d", "rtl": dict(RTL), field: bad}), fields)

    assert not leaks, "\n  ".join(leaks)


# ---------------------------------------------------------------------------------------------
# Specific inputs that once escaped as tracebacks, each named so a regression points straight at
# its cause.
# ---------------------------------------------------------------------------------------------


def test_a_bare_part_number_is_accepted_as_the_fpga_setting():
    """`fpga = "xc7a..."` is the documented shorthand, and it is accepted."""
    settings = YosysFpga.Settings(fpga="LFE5U-25F-6BG381C")  # type: ignore[arg-type]

    assert settings.fpga is not None
    assert settings.fpga.part == "LFE5U-25F-6BG381C"
    assert settings.fpga.family == "ecp5"  # the part number was also decoded


def test_a_non_string_part_is_reported_by_the_part_field():
    """The FPGA root validator called `part.strip()` before `part` was type-checked."""
    with pytest.raises(ValidationError) as excinfo:
        FPGA(part=["xc7a100t"])

    assert [e["loc"] for e in excinfo.value.errors()] == [("part",)]


@pytest.mark.parametrize("entry", ["W", 1, None], ids=repr)
def test_a_parameter_list_entry_that_is_not_an_object_is_a_design_error(entry):
    with pytest.raises(DesignValidationError, match="parameters/generics"):
        Design(name="d", rtl={**RTL, "parameters": [entry]})


def test_an_unknown_platform_lists_the_bundled_ones():
    with pytest.raises(ValueError, match=r"Unknown platform 'asap8'.*asap7.*nangate45"):
        AsicsPlatform.from_setting("asap8")


def test_a_missing_platform_file_is_a_value_error(tmp_path):
    with pytest.raises(ValueError, match="Platform file not found"):
        AsicsPlatform.from_setting(str(tmp_path / "config.toml"))


def test_a_missing_verilog_lib_names_the_file(tmp_path):
    missing = tmp_path / "cells.v"

    with pytest.raises(FlowSettingsError, match=r"cells\.v"):
        YosysFpga.Settings.from_input({"verilog_lib": [str(missing)]})


@pytest.mark.parametrize("bad", [["keep"], "keep", 3], ids=repr)
def test_set_attribute_must_be_a_mapping(bad):
    with pytest.raises(FlowSettingsError):
        YosysFpga.Settings.from_input({"set_attribute": bad})


def test_a_design_root_that_is_not_a_directory_is_a_design_error(tmp_path):
    with pytest.raises(DesignValidationError, match="directory does not exist"):
        Design(design_root=tmp_path / "missing", name="d", rtl=dict(RTL))


def test_a_design_root_that_is_not_a_path_is_a_design_error():
    with pytest.raises(DesignValidationError, match="not a path"):
        Design(design_root=123, name="d", rtl=dict(RTL))  # type: ignore[arg-type]


# ---------------------------------------------------------------------------------------------
# The command line: `-s` is an eat-all option, and repeating it must add settings, not replace them.
# ---------------------------------------------------------------------------------------------


def test_repeated_eat_all_option_accumulates():
    """`-s a=1 -s b=2` keeps both, not only `b=2` as click's default "store" action would."""

    @click.command()
    @click.option("-s", "--settings", type=tuple, cls=OptionEatAll, default=tuple())
    def command(settings):
        click.echo(" ".join(settings))

    result = CliRunner().invoke(command, ["-s", "a=1", "b=2", "-s", "c=3", "--settings", "d=4"])

    assert result.exit_code == 0, result.output
    assert result.output.split() == ["a=1", "b=2", "c=3", "d=4"]


def test_the_examples_still_load():
    """The sweep above must not have been satisfied by rejecting everything."""
    design = Design.from_file(Path(__file__).parent.parent / "examples/vhdl/sqrt/sqrt.yaml")

    assert design.rtl.sources


# ---------------------------------------------------------------------------------------------
# A `flows` table of the wrong shape, from every origin that can write one: a table maps flow
# names to mappings of settings. Any other shape is reported where it was written, never a
# traceback and never silently ignored.
# ---------------------------------------------------------------------------------------------

ORIGINS = ["command line", "API", "design file", "target", "project file"]

#: What `-s` hands over for a table or a section: text, whatever it looks like.
COMMAND_LINE_TEXTS = ["3", "[]", "x", "", "a,b", "true", "null", "{}", "[1, 2]", "a=1"]


def _writable_in_a_file(value: Any) -> bool:
    try:
        json.dumps(value)
    except (TypeError, ValueError):
        return False
    return True


def _write_design(directory: Path, **keys: Any) -> Path:
    """A design file (JSON text is YAML) with `keys` added, and the one source it names."""
    directory.mkdir(exist_ok=True)
    (directory / "top.v").write_text("module top; endmodule\n")
    path = directory / "design.yaml"
    document = {"name": "top", "rtl": {"sources": ["top.v"], "top": "top"}, **keys}
    path.write_text(json.dumps(document))
    return path


def _plan_with_flows(directory: Path, origin: str, shape: str, value: Any):
    """Plan `verilator` with `value` as the whole `flows` table (`shape` "table") or as the
    `verilator` section of one ("section"), written by `origin`."""
    runner = DefaultRunner(directory / "xeda_run", display_results=False)
    flows = value if shape == "table" else {"verilator": value}
    if origin == "command line":
        key = "flows" if shape == "table" else "flows.verilator"
        return runner.plan("verilator", _write_design(directory), flow_settings=[f"{key}={value}"])
    if origin == "API":
        return runner.plan("verilator", _write_design(directory), flow_settings={"flows": flows})
    if origin == "design file":
        return runner.plan("verilator", _write_design(directory, flows=flows))
    if origin == "target":
        design = _write_design(directory, targets={"t": {"flows": flows}})
        return runner.plan("verilator", design, target="t")
    assert origin == "project file"
    design = _write_design(directory)
    project = directory / PROJECT_FILE
    project.write_text(json.dumps({"flows": flows}))
    return runner.plan("verilator", design, xedaproject=str(project))


@pytest.mark.parametrize("shape", ["table", "section"])
@pytest.mark.parametrize("origin", ORIGINS)
def test_a_flows_table_of_the_wrong_shape_is_reported_never_a_traceback(tmp_path, origin, shape):
    if origin == "command line":
        values: list[Any] = COMMAND_LINE_TEXTS
    elif origin == "API":
        values = MALFORMED
    else:
        values = [value for value in MALFORMED if _writable_in_a_file(value)]
    leaks = []
    for number, value in enumerate(values):
        try:
            _plan_with_flows(tmp_path / str(number), origin, shape, value)
        except XedaException:
            pass
        except Exception as e:  # reporting exactly these is the point
            leaks.append(f"{value!r}: {type(e).__name__}: {e}")

    assert not leaks, f"{origin}, {shape}:\n  " + "\n  ".join(leaks)


#: shapes that are no mapping, for the table and for a section, as each origin writes them
NOT_MAPPINGS = {"command line": "3", "API": 3, "design file": 3, "target": 3, "project file": 3}


@pytest.mark.parametrize("origin", ORIGINS)
def test_a_flows_table_that_is_no_mapping_is_refused_naming_the_table(tmp_path, origin):
    with pytest.raises(XedaException, match=r"`flows` must be a mapping of flow names"):
        _plan_with_flows(tmp_path, origin, "table", NOT_MAPPINGS[origin])


@pytest.mark.parametrize("origin", ORIGINS)
def test_a_flow_section_that_is_no_mapping_is_refused_naming_the_section(tmp_path, origin):
    with pytest.raises(XedaException, match=r"`flows.verilator` must be a mapping of settings"):
        _plan_with_flows(tmp_path, origin, "section", NOT_MAPPINGS[origin])


#: What no origin takes as a table or a section, an empty list included. Code may give a flow's
#: section as `KEY=VALUE` text (see below), so the API's section is left out for the two lists of
#: such text.
REFUSED_EVEN_WHEN_EMPTY = [
    (origin, shape, value)
    for origin in ("API", "design file", "target", "project file")
    for shape in ("table", "section")
    for value in ([], [1], ["verilator.x=1"], "")
    if not (origin == "API" and shape == "section" and value in ([], ["verilator.x=1"]))
]


@pytest.mark.parametrize(
    ("origin", "shape", "value"),
    REFUSED_EVEN_WHEN_EMPTY,
    ids=[f"{origin}-{shape}-{value!r}" for origin, shape, value in REFUSED_EVEN_WHEN_EMPTY],
)
def test_a_flows_table_or_section_that_is_a_list_or_text_is_refused_even_when_empty(
    tmp_path, origin, shape, value
):
    """No origin treats an empty list or text as "no settings", and none reads a list of items as
    a table: that is how a mistake such as `flows: []` went unnoticed. Code may give a flow's
    section as `KEY=VALUE` text, and only code."""
    with pytest.raises(XedaException, match="must be a mapping"):
        _plan_with_flows(tmp_path, origin, shape, value)


@pytest.mark.parametrize("value", [[1], [{}], ["x", "y"], [1, "a=1"], ["a=1", None]], ids=repr)
def test_a_section_given_by_code_is_key_value_text_or_it_is_refused(tmp_path, value):
    with pytest.raises(XedaException, match="`flows.verilator` must be a mapping"):
        _plan_with_flows(tmp_path, "API", "section", value)


def test_code_may_give_a_flow_s_section_as_key_value_text(tmp_path):
    """The API takes a flow's settings as `KEY=VALUE` text, as the command line does."""
    plan = _plan_with_flows(tmp_path, "API", "section", ["timing=true"])

    assert plan.nodes[0].settings.timing is True


@pytest.mark.parametrize("value", [{}, None], ids=repr)
@pytest.mark.parametrize("shape", ["table", "section"])
@pytest.mark.parametrize("origin", ["API", "design file", "target", "project file"])
def test_an_empty_or_absent_flows_table_or_section_is_still_accepted(
    tmp_path, origin, shape, value
):
    """The sweep above must not have been satisfied by rejecting everything."""
    plan = _plan_with_flows(tmp_path, origin, shape, value)

    assert [node.name for node in plan.nodes] == ["verilator"]


def test_a_section_written_on_the_command_line_is_still_accepted(tmp_path):
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    plan = runner.plan(
        "verilator", _write_design(tmp_path), flow_settings=["flows.verilator.timing=true"]
    )

    assert [node.name for node in plan.nodes] == ["verilator"]
    assert plan.nodes[0].settings.timing is True


def test_a_flows_error_on_the_command_line_is_a_json_document_and_exit_status_1(tmp_path):
    design = _write_design(tmp_path)
    result = CliRunner().invoke(
        cli, ["run", "verilator", str(design), "--dry-run", "--json", "-s", "flows=3"]
    )
    document = json.loads(result.stdout)

    assert result.exit_code == 1, result.output
    assert document["success"] is False
    assert document["error"]["type"] == "FlowSettingsError"
    assert "`flows` must be a mapping" in document["error"]["message"]
