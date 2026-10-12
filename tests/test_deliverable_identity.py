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
from xeda.dataclass import DELIVERABLE_ROLE, conventional_output, written_always, written_role
from xeda.deliver import split_deliveries
from xeda.flow import flowrun_hash
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
ALWAYS_WRITTEN = {("FpgaPack.Settings", "bitstream")}

#: Deliverables the flow writes whether or not they are given, but, unset, under a name of their
#: own (`<top>.v`, `<top>.cpp`), not the conventional one a location gets. Unset and a location
#: are two runs here, as the file names differ. (The CXXRTL header is named after the model's
#: file, and a C++ driver of the design includes it by that name.)
UNSET_UNDER_OWN_NAME = {("GhdlSynth.Settings", "verilog_output"), ("CxxRtl", "filename")}


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
    for cls, _name in flow_classes():
        for path, model, field in deliverables(cls.Settings):
            if (_owner(model, field), field) in UNSET_UNDER_OWN_NAME:
                assert model.model_fields[field].default is None
                assert not written_always(model, field)
                _unset, unset = _hash(cls, tmp_path, path)
                _located, located = _hash(cls, tmp_path, path, "$PWD/one/place.out")
                assert unset != located


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
    assert results["outputs"]["bitstream"]["path"] == str(kept)

    # a name in the run directory is another file the run writes: another run
    named = launch(bitstream="board.bit")
    assert not named.reused
    assert named.outputs.bitstream == named.run_path / "board.bit"
