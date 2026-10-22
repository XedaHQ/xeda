"""Where a deliverable goes is no part of what a run is.

A run writes a deliverable under a fixed name in its run directory; a location only says where
the finished file is copied to. So a location never changes the run's identity, and a launch that
finds its run fresh still delivers. What does change the identity is whether the output is
written at all, and the name it is written under in the run directory.
"""

import json
from pathlib import Path

import pytest

from xeda import Design
from xeda.dataclass import (
    DELIVERABLE_ROLE,
    conventional_output,
    waveform_format_of,
    written_always,
    written_role,
)
from xeda.deliver import split_deliveries
from xeda.flow import FlowSettingsError, flowrun_hash
from xeda.flow.io import declared_outputs
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.launch_input import setting_naming

from . import tool_utils
from .settings_samples import flow_classes, minimal_settings
from .test_written_paths import _models

ECP5 = "LFE5U-25F-6BG381C"
BITSTREAM = b"\x00\xffXEDA bitstream\x00"

#: Deliverables the flow writes whether or not they are given, and that have no default name:
#: unset, the run writes the conventional name. (A setting with a default name is written under
#: it, and a setting that switches its output on writes nothing when unset.)
ALWAYS_WRITTEN = {
    ("FpgaPack.Settings", "bitstream"),
    ("GhdlSynth.Settings", "verilog_output"),
    ("Nextpnr.Settings", "placement"),  # Xilinx only; it reads the LUT count from the dump
}

#: Deliverables the flow writes whether or not they are given, but, unset, under a name of their
#: own, not the conventional one a location gets. Unset and a location would be two runs there.
#: There is none: a flow that needs the name for its own purpose makes the setting a working
#: location instead (`cxxrtl.filename`: the design's C++ driver includes the generated header).
UNSET_UNDER_OWN_NAME: set[tuple[str, str]] = set()


def deliverables(model, prefix=(), seen=()):
    """`(key path, owner model, field)` of every deliverable of `model`, nested models too."""
    for name, info in model.model_fields.items():
        if written_role(model, name) == DELIVERABLE_ROLE:
            yield prefix + (name,), model, name
            continue
        for nested in _models(info.annotation):
            if nested not in seen:
                yield from deliverables(nested, prefix + (name,), (*seen, nested))


def _owner(model, field):
    return next(c for c in model.__mro__ if field in getattr(c, "__annotations__", {})).__qualname__


UNSET = object()


def _hash(cls, tmp_path, path, value=UNSET):
    """The settings with the deliverable at `path` given `value` (or left alone), and their
    identity. A nested deliverable's model is always given, as a table, so that "unset" means
    the model's own default."""
    given = dict(minimal_settings(cls))
    head, *rest = path
    if rest:
        given[head] = {
            **(given.get(head) or {}),
            **({rest[0]: value} if value is not UNSET else {}),
        }
    elif value is not UNSET:
        given[head] = value
    settings = cls.Settings.from_input(given, design_root=tmp_path / "d", runner_cwd=tmp_path)
    return settings, flowrun_hash(cls.name, settings, "d")


CASES = [
    (cls, path, model, field)
    for cls, _name in flow_classes()
    for path, model, field in deliverables(cls.Settings)
]


@pytest.mark.parametrize(
    "cls, path, model, field", CASES, ids=[f"{c.name}.{'.'.join(p)}" for c, p, _m, _f in CASES]
)
def test_a_deliverable_s_location_is_no_part_of_the_run_s_identity(
    tmp_path, cls, path, model, field
):
    """Unset, and two different locations: the locations are one run, and so is unset when the
    flow writes the output anyway. When unset writes nothing, giving a location is a different
    run: it writes a file."""
    info = model.model_fields[field]
    written_unset = info.default is not None or written_always(model, field)
    _unset, unset = _hash(cls, tmp_path, path)
    first_settings, first = _hash(cls, tmp_path, path, "$PWD/one/place.out")
    _second_settings, second = _hash(cls, tmp_path, path, "$PWD/another/other.out")
    assert first == second
    assert (unset == first) == written_unset, (
        f"{'.'.join(path)}: unset is "
        + ("not " if not written_unset else "")
        + "written, so it is "
        + ("not " if not written_unset else "")
        + "the location's run"
    )
    # the conventional name with `{design}` in it stands for that name, for every deliverable,
    # and `true` is that name for a waveform
    template = conventional_output(model, field, "{design}")
    if template is not None and "{design}" in str(template):
        _named, named = _hash(cls, tmp_path, path, str(template))
        assert named == first
    if waveform_format_of(model, field) is not None:
        _on, on = _hash(cls, tmp_path, path, True)
        assert on == first
    # the launcher turns each location into its conventional name before the identity: the same
    # run again
    split_deliveries(first_settings, "d")
    assert flowrun_hash(cls.name, first_settings, "d") == first


def test_the_written_always_markers_are_the_declared_table():
    marked = {
        (_owner(model, field), field)
        for cls, _name in flow_classes()
        for _path, model, field in deliverables(cls.Settings)
        if written_always(model, field)
    }
    assert marked == ALWAYS_WRITTEN


def test_a_deliverable_written_under_its_own_name_is_not_marked(tmp_path):
    """Such a deliverable's identity differs from a location's: the run writes another file."""
    matched = set()
    for cls, _name in flow_classes():
        for path, model, field in deliverables(cls.Settings):
            if (_owner(model, field), field) in UNSET_UNDER_OWN_NAME:
                matched.add((_owner(model, field), field))
                assert model.model_fields[field].default is None
                assert not written_always(model, field)
                _unset, unset = _hash(cls, tmp_path, path)
                _located, located = _hash(cls, tmp_path, path, "$PWD/one/place.out")
                assert unset != located
    assert matched == UNSET_UNDER_OWN_NAME


#: Where a flow's code gives an unset deliverable (default `None`) a name of its own, by file
#: and setting. Each is either a deliverable the flow writes in any case (see `ALWAYS_WRITTEN`),
#: or a demand that names the output (`enable_output`: a consumer that needs the file sets its
#: conventional name).
REVIEWED_FALLBACKS = {
    ("flows/fpga_pack.py", "bitstream"): "always written; conventional name",
    ("flows/ghdl/__init__.py", "verilog_output"): "always written; conventional name",
    ("flows/vivado/vivado_synth.py", "bitstream"): "a consumer's demand names it",
    ("flows/vivado/vivado_postsynthsim.py", "saif"): "a consumer's demand names it",
}


def _fallbacks(fields):
    """`(file, setting)` for each place a flow's code gives one of `fields` another value when it
    is unset: `x.field or ...` outside a condition, and `if x.field is None:` or `if not x.field:`
    that assigns `x.field`. A syntactic scan: it cannot see a fallback made by other means (a
    default chosen in a helper, a template), so it narrows the review and does not replace it."""
    import ast

    import xeda

    root = Path(xeda.__file__).parent
    found = set()
    for source in sorted((root / "flows").rglob("*.py")):
        tree = ast.parse(source.read_text())
        conditions = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.If, ast.IfExp, ast.While, ast.Assert)):
                conditions.add(id(node.test))
            if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.Not):
                conditions.add(id(node.operand))
            if isinstance(node, ast.comprehension):
                conditions.update(id(test) for test in node.ifs)
        relative = str(source.relative_to(root))
        for node in ast.walk(tree):
            if isinstance(node, ast.BoolOp) and isinstance(node.op, ast.Or):
                first = node.values[0]
                if id(node) not in conditions and getattr(first, "attr", None) in fields:
                    found.add((relative, first.attr))
            if not isinstance(node, ast.If):
                continue
            test = node.test
            if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
                subject = test.operand  # `if not x.field:`
            elif (
                isinstance(test, ast.Compare)
                and isinstance(test.ops[0], ast.Is)
                and getattr(test.comparators[0], "value", 0) is None
            ):
                subject = test.left  # `if x.field is None:`
            else:
                continue
            if isinstance(subject, ast.Attribute) and subject.attr in fields:
                for inner in ast.walk(ast.Module(body=node.body, type_ignores=[])):
                    if isinstance(inner, ast.Assign) and any(
                        isinstance(target, ast.Attribute) and target.attr == subject.attr
                        for target in inner.targets
                    ):
                        found.add((relative, subject.attr))
    return found


def test_a_flow_gives_an_unset_deliverable_a_name_only_where_it_was_reviewed():
    """A flow that writes an output whether or not its setting is given names it in its code
    when the setting is unset. Such a place is reviewed: the output is marked written always,
    or listed with the name it gets, or a consumer's demand names it."""
    fields = {
        field
        for cls, _name in flow_classes()
        for _path, model, field in deliverables(cls.Settings)
        if model.model_fields[field].default is None
    }
    assert _fallbacks(fields) == set(REVIEWED_FALLBACKS)


def test_a_written_always_deliverable_has_no_default_and_a_conventional_name():
    for cls, _name in flow_classes():
        for path, model, field in deliverables(cls.Settings):
            if written_always(model, field):
                where = f"{cls.name}.{'.'.join(path)}"
                assert model.model_fields[field].default is None, where
                assert conventional_output(model, field, "d") is not None, where


def test_every_output_a_flow_always_declares_is_written_always_when_it_has_no_default():
    """A declared output with no switch is written by every run. The setting that names it is
    then marked, unless it has a default name."""
    for cls, _name in flow_classes():
        for declaration in declared_outputs(cls).values():
            if declaration.enabled_by is not None or declaration.cardinality != "one":
                continue
            setting = setting_naming(cls, declaration.name)
            if setting is None:
                continue
            info = cls.Settings.model_fields[setting]
            if info.default is None:
                assert written_always(cls.Settings, setting), f"{cls.name}.{setting}"


# ------------------------------------------------------------------ a launch, end to end


@pytest.fixture
def toolchain(tmp_path, monkeypatch):
    prefix = tool_utils.use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")
    monkeypatch.chdir(tmp_path)
    return prefix


def _design(tmp_path: Path) -> Design:
    root = tmp_path / "design"
    root.mkdir(exist_ok=True)
    (root / "top.v").write_text("module top(input clk, output q); assign q = clk; endmodule\n")
    return Design(name="top", design_root=root, rtl={"sources": ["top.v"], "top": "top"})


def _tools_started(tmp_path: Path) -> int:
    return sum(
        len((record).read_text().splitlines())
        for record in (tmp_path / "run").glob("top/*/fake_fpga.calls.jsonl")
    )


def test_a_location_given_to_a_finished_run_delivers_without_running_again(tmp_path, toolchain):
    design = _design(tmp_path)
    runner = DefaultRunner(tmp_path / "run", display_results=False)

    def launch(**settings):
        flow = runner.run("fpga_pack", design, flow_settings={"fpga": ECP5, **settings})
        assert flow is not None and flow.succeeded
        return flow

    first = launch()
    assert not first.reused
    started = _tools_started(tmp_path)
    kept = first.run_path / "outputs/top.bit"
    assert kept.read_bytes() == BITSTREAM

    here, there = tmp_path / "here" / "a.bit", tmp_path / "there" / "b.bit"
    for location in (here, there):
        location.parent.mkdir()
        again = launch(bitstream=str(location))
        assert again.reused and again.run_path == first.run_path
        assert location.read_bytes() == BITSTREAM
    # taking the location back, or moving it, is the same run
    assert launch().reused
    assert launch(bitstream=str(here)).reused
    assert _tools_started(tmp_path) == started, "no tool started again"
    results = json.loads((first.run_path / "results.json").read_text())
    assert results["outputs"]["bitstream"]["names"] == [kept.relative_to(first.run_path).as_posix()]

    # a name in the run directory is another file the run writes: another run
    named = launch(bitstream="board.bit")
    assert not named.reused
    assert named.outputs.bitstream == named.run_path / "board.bit"


SWITCHES = ("true", "false", "TRUE", True, False)


@pytest.mark.parametrize(
    "cls, path, model, field", CASES, ids=[f"{c.name}.{'.'.join(p)}" for c, p, _m, _f in CASES]
)
def test_true_and_false_name_no_file(tmp_path, cls, path, model, field):
    """A deliverable that has no switch meaning refuses `true` and `false`, as text and as a
    boolean, and says to give a location. One that reads them as a switch (a waveform) takes them.
    """
    for value in SWITCHES:
        if waveform_format_of(model, field) is not None:
            _hash(cls, tmp_path, path, value)
            continue
        with pytest.raises(FlowSettingsError) as refused:
            _hash(cls, tmp_path, path, value)
        message = str(refused.value)
        assert f"`{str(value).lower()}` is a switch" in message and path[-1] in message
        assert "location" in message


def test_ghdl_synth_writes_its_conventional_name_when_unset(tmp_path):
    """The run writes `outputs/<design>.v` whether `verilog_output` is given or not, so a location
    given later is delivered from the same run."""
    tool_utils.require_ghdl()
    root = tmp_path / "design"
    root.mkdir()
    (root / "inv.vhd").write_text(
        "library ieee; use ieee.std_logic_1164.all;\n"
        "entity inv is port(a: in std_logic; y: out std_logic); end;\n"
        "architecture rtl of inv is begin y <= not a; end;\n"
    )
    design = Design(name="inv", design_root=root, rtl={"sources": ["inv.vhd"], "top": "inv"})
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    first = runner.run("ghdl_synth", design)
    assert first is not None and first.succeeded
    written = first.run_path / "outputs/inv.v"
    assert written.is_file() and first.artifacts.generated_verilog == [Path("outputs/inv.v")]
    location = tmp_path / "delivered" / "inverter.v"
    location.parent.mkdir()
    again = runner.run("ghdl_synth", design, flow_settings={"verilog_output": str(location)})
    assert again is not None and again.reused
    assert location.read_bytes() == written.read_bytes()
