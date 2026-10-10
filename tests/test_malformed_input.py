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

#: Where a `flows` table is written. "design with a target" is a design file whose own table is
#: the one under test, with a target that writes a good `flows` mapping of its own and is
#: selected: the merge of that mapping must not hide the design's table.
ORIGINS = [
    "command line",
    "API",
    "design file",
    "design with a target",
    "target",
    "project file",
]

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


#: Two targets: `a` writes a good `flows` mapping, `b` writes none.
TARGETS_WITH_FLOWS = {"a": {"flows": {"verilator": {"timing": True}}}, "b": {}}


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
    if origin == "design with a target":
        design = _write_design(directory, flows=flows, targets=TARGETS_WITH_FLOWS)
        return runner.plan("verilator", design, target="a")
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
NOT_MAPPINGS = {
    "command line": "3",
    "API": 3,
    "design file": 3,
    "design with a target": 3,
    "target": 3,
    "project file": 3,
}


@pytest.mark.parametrize("origin", ORIGINS)
def test_a_flows_table_that_is_no_mapping_is_refused_naming_the_table(tmp_path, origin):
    with pytest.raises(XedaException, match=r"`flows` must be a mapping of flow names"):
        _plan_with_flows(tmp_path, origin, "table", NOT_MAPPINGS[origin])


@pytest.mark.parametrize("origin", ORIGINS)
def test_a_flow_section_that_is_no_mapping_is_refused_naming_the_section(tmp_path, origin):
    with pytest.raises(XedaException, match=r"`flows.verilator` must be a mapping of settings"):
        _plan_with_flows(tmp_path, origin, "section", NOT_MAPPINGS[origin])


#: Keys of a `flows` table that name no flow. A file or `-s` always writes text; code may not.
NOT_FLOW_NAMES = [3, None, 1.5, ("verilator",)]


@pytest.mark.parametrize("key", NOT_FLOW_NAMES, ids=repr)
@pytest.mark.parametrize("door", ["plan", "run_flow", "launch_flow"])
def test_a_flows_key_that_is_no_flow_name_is_refused_naming_it(tmp_path, door, key):
    """Code can give a `flows` table whose key is not text; it is refused like any other
    malformed table, never left to fail deeper in the merge."""
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    design_file = _write_design(tmp_path / "d")
    with pytest.raises(XedaException, match=r"`flows` has the key .*, which is no flow name"):
        if door == "plan":
            runner.plan("verilator", design_file, flow_settings={"flows": {key: {}}})
        elif door == "run_flow":
            runner.run_flow(
                "verilator", Design.from_file(design_file), {}, all_flows_settings={key: {}}
            )
        else:
            runner.launch_flow(
                "verilator", Design.from_file(design_file), {}, all_flows_settings={key: {}}
            )


#: What no origin takes as a table or a section, an empty list included. Code may give a flow's
#: section as `KEY=VALUE` text (see below), so the API's section is left out for the two lists of
#: such text.
REFUSED_EVEN_WHEN_EMPTY = [
    (origin, shape, value)
    for origin in ("API", "design file", "design with a target", "target", "project file")
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
@pytest.mark.parametrize(
    "origin", ["API", "design file", "design with a target", "target", "project file"]
)
def test_an_empty_or_absent_flows_table_or_section_is_still_accepted(
    tmp_path, origin, shape, value
):
    """The sweep above must not have been satisfied by rejecting everything."""
    plan = _plan_with_flows(tmp_path, origin, shape, value)

    assert [node.name for node in plan.nodes] == ["verilator"]


@pytest.mark.parametrize("shape", ["table", "section"])
def test_the_design_s_own_table_is_judged_whichever_target_is_selected(tmp_path, shape):
    """A target that writes a `flows` mapping merges it over the design's table, and the merge
    replaces whatever is not a mapping: a design's mistake was hidden for that target alone, and
    reported for another. The design is as valid as the table it holds, whatever is selected."""
    disagreements = []
    for number, value in enumerate(v for v in MALFORMED if _writable_in_a_file(v)):
        flows = value if shape == "table" else {"verilator": value}
        outcomes = {}
        for target in TARGETS_WITH_FLOWS:
            directory = tmp_path / f"{number}-{target}"
            design = _write_design(directory, flows=flows, targets=TARGETS_WITH_FLOWS)
            runner = DefaultRunner(directory / "xeda_run", display_results=False)
            try:
                runner.plan("verilator", design, target=target)
                outcomes[target] = "accepted"
            except XedaException as e:
                outcomes[target] = type(e).__name__
        if outcomes["a"] != outcomes["b"]:
            disagreements.append(f"{value!r}: {outcomes}")

    assert not disagreements, "\n  ".join(disagreements)


@pytest.mark.parametrize("selected", ["a", "b"])
@pytest.mark.parametrize("key", ["flows", "flow"])
def test_a_malformed_table_of_a_target_is_refused_at_the_target_whichever_is_selected(
    tmp_path, selected, key
):
    """Every target's overlay is checked, selected or not, and the mistake is located in the
    target that wrote it, not in the merged design."""
    design = _write_design(tmp_path, targets={"a": {key: {"verilator": 3}}, "b": {}})
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)

    with pytest.raises(DesignValidationError) as refused:
        runner.plan("verilator", design, target=selected)

    assert [location for location, *_ in refused.value.errors] == [f"targets.a.{key}"]
    assert "`flows.verilator` must be a mapping of settings" in str(refused.value)


def test_a_section_written_on_the_command_line_is_still_accepted(tmp_path):
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    plan = runner.plan(
        "verilator", _write_design(tmp_path), flow_settings=["flows.verilator.timing=true"]
    )

    assert [node.name for node in plan.nodes] == ["verilator"]
    assert plan.nodes[0].settings.timing is True


@pytest.mark.parametrize(
    ("setting", "field"),
    [
        ("flows=3", "`flows`"),
        ("flows=[]", "`flows`"),
        ("flows.verilator=3", "`flows.verilator`"),
    ],
)
def test_a_flows_error_on_the_command_line_is_a_json_document_and_exit_status_1(
    tmp_path, setting, field
):
    design = _write_design(tmp_path)
    run_root = tmp_path / "xeda_run"
    result = CliRunner().invoke(
        cli,
        [
            "run",
            "verilator",
            str(design),
            "--dry-run",
            "--json",
            "--run-root",
            str(run_root),
            "-s",
            setting,
        ],
    )
    document = json.loads(result.stdout)

    assert result.exit_code == 1, result.output
    assert document["success"] is False
    assert document["error"]["type"] == "FlowSettingsError"
    assert field in document["error"]["message"]
    assert "must be a mapping" in document["error"]["message"]
    assert "Traceback" not in result.output
    assert not run_root.exists(), "validation failed before creating a run root"


# ---------------------------------------------------------------------------------------------
# A key given as a value and as a table, from every origin that expands dotted keys: `-s
# timing=true timing.x=1`, `--design-overrides tb=3 tb.top=x`, a design file with `tb: 3` beside
# `tb.top`, a `flows` table with `verilator: 3` beside `verilator.timing`. A reported error naming
# both keys in either order -- never a traceback, never a table silently lost.
# ---------------------------------------------------------------------------------------------

#: (the key that holds a value, a key inside it): setting names, design keys and flow sections
KEY_PAIRS = [
    ("timing", "timing.x"),
    ("flows", "flows.verilator.timing"),
    ("flows.verilator", "flows.verilator.timing"),
    ("clock", "clock.period"),
    ("tb", "tb.top"),
]
#: what the value key is given: text the command line can write, and what a file or code can
VALUES_OF_THE_KEY = ["3", "true", "x", "", "[]"]
KEY_ORIGINS = [
    "command line",
    "design overrides",
    "API",
    "design file",
    "target",
    "flows table of a design",
    "flows table of the API",
    "flows table of a project",
    "section of a flows table",
    "target flows table",
]


def _plan_with_keys(
    directory: Path, origin: str, value_key: str, child_key: str, value: Any, child_first: bool
):
    """Plan `verilator` where `origin` gives `value_key` the `value` and `child_key` the number 1,
    in the order asked for."""
    runner = DefaultRunner(directory / "xeda_run", display_results=False)
    items = [(value_key, value), (child_key, 1)]
    if child_first:
        items.reverse()
    texts = [f"{key}={item}" for key, item in items]
    if origin == "command line":
        return runner.plan("verilator", _write_design(directory), flow_settings=texts)
    if origin == "design overrides":
        return runner.plan("verilator", _write_design(directory), design_overrides=texts)
    if origin == "API":
        return runner.plan("verilator", _write_design(directory), flow_settings=dict(items))
    if origin == "design file":
        return runner.plan("verilator", _write_design(directory, **dict(items)))
    if origin == "target":
        design = _write_design(directory, targets={"a": dict(items), "b": {}})
        return runner.plan("verilator", design, target="a")
    flow_items = [("verilator", value), ("verilator.timing", True)]
    if child_first:
        flow_items.reverse()
    if origin == "flows table of a design":
        return runner.plan("verilator", _write_design(directory, flows=dict(flow_items)))
    if origin == "flows table of the API":
        return runner.plan(
            "verilator", _write_design(directory), flow_settings={"flows": dict(flow_items)}
        )
    if origin == "flows table of a project":
        design = _write_design(directory)
        project = directory / PROJECT_FILE
        project.write_text(json.dumps({"flows": dict(flow_items)}))
        return runner.plan("verilator", design, xedaproject=str(project))
    if origin == "section of a flows table":
        section = [("clock", value), ("clock.period", 5)]
        if child_first:
            section.reverse()
        return runner.plan(
            "verilator", _write_design(directory, flows={"verilator": dict(section)})
        )
    assert origin == "target flows table"
    design = _write_design(directory, targets={"a": {"flows": dict(flow_items)}, "b": {}})
    return runner.plan("verilator", design, target="a")


@pytest.mark.parametrize("child_first", [False, True], ids=["value-first", "child-first"])
@pytest.mark.parametrize("origin", KEY_ORIGINS)
def test_a_key_that_is_a_value_and_a_table_is_reported_by_every_origin(
    tmp_path, origin, child_first
):
    pairs = KEY_PAIRS if "flows table" not in origin else KEY_PAIRS[:1]
    escaped = []
    for number, ((value_key, child_key), value) in enumerate(
        (pair, value) for pair in pairs for value in VALUES_OF_THE_KEY
    ):
        if origin == "design file" and value == "[]":
            continue  # a design file's `[]` is a list, as the sweep above covers
        try:
            _plan_with_keys(
                tmp_path / str(number), origin, value_key, child_key, value, child_first
            )
        except XedaException:
            continue
        except Exception as e:  # reporting exactly these is the point
            escaped.append(f"{value_key}={value!r} {child_key}: {type(e).__name__}: {e}")
            continue
        escaped.append(f"{value_key}={value!r} {child_key}: accepted, one of them lost")

    assert not escaped, f"{origin}:\n  " + "\n  ".join(escaped)


def test_the_command_line_names_both_keys_and_exits_with_status_1(tmp_path):
    design = _write_design(tmp_path)
    for order in (["flows=3", "flows.verilator.timing=true"], ["timing.x=1", "timing=true"]):
        result = CliRunner().invoke(
            cli, ["run", "verilator", str(design), "--dry-run", "--json", "-s", *order]
        )
        document = json.loads(result.stdout)

        assert result.exit_code == 1, result.output
        assert document["error"]["type"] == "ConflictingKeys"
        assert "sets a key inside it: give one of them" in document["error"]["message"]


def test_a_design_file_with_such_keys_is_a_design_error_naming_the_file(tmp_path):
    design = _write_design(tmp_path, **{"tb": 3, "tb.top": "x"})
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)

    with pytest.raises(DesignValidationError, match=r"`tb` is set to a value.*`tb.top`") as raised:
        runner.plan("verilator", design)

    assert raised.value.file == str(design.absolute())


@pytest.mark.parametrize("selected", ["a", "b"])
def test_a_target_with_such_keys_is_refused_at_the_target_whichever_is_selected(tmp_path, selected):
    design = _write_design(tmp_path, targets={"a": {"tb": 3, "tb.top": "x"}, "b": {}})
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)

    with pytest.raises(DesignValidationError) as raised:
        runner.plan("verilator", design, target=selected)

    assert [location for location, *_ in raised.value.errors] == ["targets.a.tb"]
    assert "`targets.a.tb` is set to a value, and `targets.a.tb.top` sets a key" in str(
        raised.value
    )
