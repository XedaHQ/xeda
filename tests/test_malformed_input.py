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

from pathlib import Path

import click
import pytest
from click.testing import CliRunner

from xeda.cli_utils import OptionEatAll
from xeda.dataclass import ValidationError
from xeda.design import Design, DesignValidationError
from xeda.flow import FPGA, FlowSettingsError
from xeda.flow.flow import registered_flows
from xeda.flows import __builtin_flows__
from xeda.flows.yosys.yosys_fpga import YosysFpga
from xeda.platforms.asics import AsicsPlatform

assert __builtin_flows__, "importing `xeda.flows` is what populates `registered_flows`"

#: What a malformed value may raise. `ValueError` is included because `FlowSettingsError`,
#: `DesignValidationError` and pydantic's `ValidationError` all derive from it.
VALIDATION_ERRORS = (ValidationError, FlowSettingsError, DesignValidationError, ValueError)

MALFORMED = [123, 4.5, True, "x", ["x"], [1, 2], {"a": 1}, {"a": {"b": 1}}, object(), None, [{}]]

MINIMAL_SETTINGS = {
    "fpga": {"part": "xc7a100tcsg324-1"},
    "clock_period": 5.0,
    "platform": "asap7",
    "target_libraries": ["nangate45.lib"],
}

RTL = {"sources": [], "top": "t"}


def _flow_classes():
    classes = {}
    for _, (_, cls) in sorted(registered_flows.items()):
        if not cls.__name__.startswith("_"):  # test-only flows registered by other modules
            classes.setdefault(cls, cls.name)
    return sorted(classes.items(), key=lambda kv: kv[1])


FLOWS = _flow_classes()


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
    base = {k: v for k, v in MINIMAL_SETTINGS.items() if k in cls.Settings.model_fields}
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
# Specific inputs that used to escape, each named so a regression points straight at its cause.
# ---------------------------------------------------------------------------------------------


def test_a_bare_part_number_is_accepted_as_the_fpga_setting():
    """`fpga = "xc7a..."` is the documented shorthand, and it used to be rejected."""
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
        YosysFpga.Settings(verilog_lib=[str(missing)])  # type: ignore[call-arg]


@pytest.mark.parametrize("bad", [["keep"], "keep", 3], ids=repr)
def test_set_attribute_must_be_a_mapping(bad):
    with pytest.raises(FlowSettingsError):
        YosysFpga.Settings(set_attribute=bad)  # type: ignore[call-arg]


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
    """`-s a=1 -s b=2` used to keep only `b=2` -- click's default "store" action."""

    @click.command()
    @click.option("-s", "--settings", type=tuple, cls=OptionEatAll, default=tuple())
    def command(settings):
        click.echo(" ".join(settings))

    result = CliRunner().invoke(command, ["-s", "a=1", "b=2", "-s", "c=3", "--settings", "d=4"])

    assert result.exit_code == 0, result.output
    assert result.output.split() == ["a=1", "b=2", "c=3", "d=4"]


def test_the_examples_still_load():
    """The sweep above must not have been satisfied by rejecting everything."""
    design = Design.from_toml(Path(__file__).parent.parent / "examples/vhdl/sqrt/sqrt.toml")

    assert design.rtl.sources
