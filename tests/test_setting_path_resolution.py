"""A relative path in a setting that a flow reads is relative to the design root, for the tool too.

The tool runs in the run directory. So the flow gets each such path absolute, resolved once when
it is built (`resolve_setting_paths`), by the reader that the trace and the mounts of a container
use. A flow does not resolve a path of its own. A new path setting is covered at once: the first
test builds every flow with a relative value in every setting that it reads.
"""

import json
import typing
from pathlib import Path
from types import UnionType
from typing import Annotated, Any, ClassVar, Union, get_args, get_origin

import pytest
from pydantic import BaseModel, Field

from xeda import Design
from xeda._internal.flow import MetricSpec
from xeda._internal.flow_runner import DefaultRunner
from xeda._internal.flow import flowrun_hash as flow_run_hash
from xeda._internal.dataclass import is_locator, written_role
from xeda._internal.flow.flow import (
    Flow,
    _annotation_contains_path,
    _is_path_annotation,
    _nested_models,
    setting_path_target,
)
from xeda._internal.flow_runner.trace_inputs import setting_candidates, setting_path_leaves
from xeda._internal.flows import Nvc
from xeda._internal.introspect import all_flow_classes

from .flow_registry import preserved_flow_registry
from .scoping import node_settings
from .test_openroad_final import final_flow, render_final
from .test_tcl_paths import _calls, _design, needs_tclsh

pytestmark = pytest.mark.python_compat

#: The settings that a test cannot assign a sample to, with the reason. A new entry needs a
#: reason that is not "it failed".
NOT_SAMPLED = {
    ("vivado_alt_synth", "tcl_files"): "the flow refuses the setting (no Tcl hooks in its mode)",
}

#: The path fields that only a list or a table of models holds, by flow. They are empty in the
#: default settings, so the check cannot assign them. A case of their own covers each.
UNREACHED: dict[str, set[str]] = {
    "yosys": {"ghdl.lib_paths", "techmap_files.path"},
    "yosys_fpga": {"ghdl.lib_paths"},
    "yosys_sim": {"ghdl.lib_paths"},
}

#: The value of a key that its table accepts only at known names.
KNOWN_KEYS = {"hooks": "pre_elab"}


def _sample(annotation: Any, name: str, root: Path) -> Any:
    """Return a relative value that fits `annotation`, and make the file it names.

    The value is one string, or the container that the shape of the annotation asks for."""
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        return _sample(args[0], name, root)
    if _is_path_annotation(annotation):
        (root / "rel").mkdir(exist_ok=True)
        (root / "rel" / f"{name}.yaml").write_text("{}\n")
        return f"rel/{name}.yaml"
    if origin in (Union, UnionType):
        for choice in args:
            if _annotation_contains_path(choice):
                return _sample(choice, name, root)
        return None
    if annotation is str:
        return name
    if annotation is type(None):
        return None
    if origin in (list, set, frozenset):
        return [_sample(args[0], name, root)]
    if origin is tuple:
        return tuple(_sample(item, name, root) for item in args)
    if origin is dict:
        key = KNOWN_KEYS.get(name, "key")
        return {key: _sample(args[1], name, root)}
    raise TypeError(f"no sample for {annotation}")


def _read_path_fields(model: type[BaseModel], prefix: str = "") -> dict[str, type[BaseModel]]:
    """Return the path fields that a flow reads in `model` and in the models nested in it, by key
    path, found from the types alone."""
    found: dict[str, type[BaseModel]] = {}

    def nested(annotation: Any) -> typing.Iterator[type[BaseModel]]:
        for arg in get_args(annotation):
            yield from nested(arg)
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            yield annotation

    for name, info in model.model_fields.items():
        if is_locator(model, name):
            continue
        if written_role(model, name) is None and _annotation_contains_path(info.annotation):
            found[prefix + name] = model
        for sub in nested(info.annotation):
            found.update(_read_path_fields(sub, f"{prefix}{name}."))
    return found


def _fill(model: BaseModel, root: Path, assigned: set[str], flow: str, prefix: str = "") -> None:
    """Give every path field that the flow reads, in `model` and in the models in it, a relative
    value. A field with a reason (`NOT_SAMPLED`) gets none."""
    for name, info in type(model).model_fields.items():
        if is_locator(type(model), name):
            continue
        if (
            written_role(type(model), name) is None
            and _annotation_contains_path(info.annotation)
            and (flow, prefix + name) not in NOT_SAMPLED
        ):
            # The validators read the design root from the context, as they do when the settings
            # are built; an assignment alone gives them none.
            type(model).__pydantic_validator__.validate_assignment(
                model,
                name,
                _sample(info.annotation, name, root),
                context={"design_root": root, "runner_cwd": root},
            )
            assigned.add(prefix + name)
        for key, item in _nested_models(getattr(model, name), prefix + name):
            _fill(item, root, assigned, flow, key + ".")


def _flow_for(cls: type[Flow], root: Path, run_path: Path, settings: Flow.Settings) -> Flow:
    """Build `cls` in `root` with `settings`; the design holds one source that the flow reads."""
    kind = sorted(member.name for member in cls.reads_sources or []) or ["Verilog"]
    suffix = {"Bluespec": "bsv"}.get(kind[0], "src")
    (root / f"x.{suffix}").write_text("x\n")
    design = Design(
        name="d",
        root=root,
        rtl={"sources": [{"file": f"x.{suffix}", "type": kind[0]}], "top": "t"},
        tb={"top": "t", "uut": "u"},
    )
    return cls(settings, design, run_path)


@pytest.mark.parametrize("flow_class", all_flow_classes(), ids=lambda cls: cls.name)
def test_every_path_setting_a_flow_reads_is_absolute_under_the_design_root(flow_class, tmp_path):
    """A relative value in each such setting reaches the settings of the flow as the path under
    the design root, the place that the trace reads (`setting_candidates`)."""
    root = tmp_path / "design"
    root.mkdir()
    root = root.resolve()
    given = flow_class.Settings.from_input({}, design_root=root, runner_cwd=tmp_path)
    assigned: set[str] = set()
    _fill(given, root, assigned, flow_class.name)
    asked = setting_path_leaves(given, written=False)
    assert any(not Path(leaf).is_absolute() for _, leaf in asked), flow_class.name
    flow = _flow_for(flow_class, root, tmp_path / "run", given)
    leaves = setting_path_leaves(flow.settings, written=False)
    assert [key for key, _ in leaves] == [key for key, _ in asked]
    for key, leaf in leaves:
        assert Path(leaf).is_absolute() and Path(leaf).is_relative_to(root), (key, leaf)
    # The tool and the trace read the same place.
    assert {key: Path(leaf) for key, leaf in leaves} == dict(setting_candidates(given))
    # The settings that the caller gave stay as given.
    assert setting_path_leaves(given, written=False) == asked


@pytest.mark.parametrize("flow_class", all_flow_classes(), ids=lambda cls: cls.name)
def test_no_path_setting_is_left_out_of_the_check_above(flow_class, tmp_path):
    """Every path field that the types show is assigned in the check above or has a reason. A
    field that only a list or a table of models holds is reached by the types and not by the
    values, so it needs a case of its own."""
    root = tmp_path / "design"
    root.mkdir()
    given = flow_class.Settings.from_input({}, design_root=root, runner_cwd=tmp_path)
    assigned: set[str] = set()
    _fill(given, root, assigned, flow_class.name)
    known = {key for name, key in NOT_SAMPLED if name == flow_class.name}
    missing = set(_read_path_fields(flow_class.Settings)) - assigned - known
    assert missing == UNREACHED.get(flow_class.name, set()), sorted(missing)


@pytest.mark.parametrize("flow_name", ["yosys", "yosys_fpga", "yosys_sim"])
def test_the_paths_in_a_nested_group_or_list_of_models_are_resolved(flow_name, tmp_path):
    """The GHDL plugin settings inside those of yosys, and a list of maps, are read as the others."""
    root = tmp_path / "design"
    root.mkdir()
    root = root.resolve()
    flow_class = next(cls for cls in all_flow_classes() if cls.name == flow_name)
    given = {"ghdl": {"lib_paths": [["vendor", "libs"]]}}
    if flow_name == "yosys":
        given["techmap_files"] = ["maps/a.v"]
    settings = flow_class.Settings.from_input(given, design_root=root, runner_cwd=tmp_path)
    flow = _flow_for(flow_class, root, tmp_path / "run", settings)
    found = dict(setting_path_leaves(flow.settings, written=False))
    assert Path(found["ghdl.lib_paths[0][1]"]) == root / "libs"
    if flow_name == "yosys":
        assert Path(found["techmap_files[0].path"]) == root / "maps" / "a.v"


def test_a_path_that_names_no_design_place_stays_as_given():
    """A path with a variable that nothing expanded, and a `+/` path of yosys, name no place."""
    root = Path("/design")
    assert setting_path_target("$UNSET/x.v", root) is None
    assert setting_path_target("+/xilinx/cells_sim.v", root) is None
    assert setting_path_target("a/b.v", root) == root / "a" / "b.v"
    assert setting_path_target("/abs/b.v", root) == Path("/abs/b.v")
    assert setting_path_target("a/b.v", None) is None


def test_nvc_gets_the_cover_spec_under_the_design_root(tmp_path):
    """`cover_spec` was handed to NVC as written, and NVC runs in the run directory."""
    root = tmp_path / "design"
    root.mkdir()
    (root / "tb.vhd").write_text("entity tb is end;\n")
    (root / "cover.spec").write_text("")
    design = Design(name="d", root=root, rtl={"sources": ["tb.vhd"], "top": "tb"}, tb={"top": "tb"})
    flow = Nvc({"cover": ["statement"], "cover_spec": "cover.spec"}, design, tmp_path / "run")
    flags = flow.elaborate_flags()
    assert flags[flags.index("--cover-spec") + 1] == str(design.root / "cover.spec")


@needs_tclsh
def test_a_tcl_hook_reaches_vivado_from_the_design_root(tmp_path, monkeypatch):
    """`tcl_files` was sourced as written, from the run directory."""
    root = tmp_path / "design"
    design = _design(root)
    (root / "hooks").mkdir()
    hook = root / "hooks" / "after.tcl"
    hook.write_text("# a hook\n")
    (tmp_path / "start").mkdir()
    (tmp_path / "start" / "here.tcl").write_text("# a hook\n")
    monkeypatch.chdir(tmp_path / "start")
    settings = {
        "fpga": {"part": "xc7a12tcsg325-1"},
        "clocks": {"main_clock": {"period": "10 ns"}},
        "tcl_files": ["hooks/after.tcl", "$DESIGN_ROOT/hooks/after.tcl", "$PWD/here.tcl"],
    }
    calls = _calls("vivado_synth", design, settings, tmp_path, monkeypatch, elements=True)
    arguments = [arg for call in calls for arg in call]
    # A relative path is the design root's; a variable is expanded, as it was before.
    assert arguments.count(str(design.root / "hooks" / "after.tcl")) >= 2
    assert str(tmp_path.resolve() / "start" / "here.tcl") in arguments


def test_the_rules_file_of_openroad_is_read_from_the_design_root(tmp_path):
    """`extraction.rules_file` was checked against the start directory, and OpenROAD read it from
    the run directory."""
    (tmp_path / "design").mkdir(exist_ok=True)
    (tmp_path / "design" / "mine.rules").write_text("my rules\n")
    flow = final_flow(tmp_path, {"extraction": {"rules_file": "mine.rules"}})
    design_root = flow.design.root
    assert flow.settings.extraction.rules_file == design_root / "mine.rules"
    assert flow.effective().rcx_rules == design_root / "mine.rules"
    assert f'set_extraction_rules_file "{design_root / "mine.rules"}"' in render_final(flow)


@pytest.fixture
def hook_flow():
    """A flow that copies the file of its `data_file` setting, and records the path it was given."""
    seen: list[Path] = []

    class TProbeHook(Flow):
        """Copies the file that `data_file` names to an output."""

        results_description: ClassVar[dict[str, MetricSpec]] = {}

        class Settings(Flow.Settings):
            data_file: Path | None = Field(None, description="A file that the flow reads.")

        def run(self) -> None:
            assert self.settings.data_file is not None
            seen.append(self.settings.data_file)
            out = self.run_path / "outputs" / "copy.txt"
            out.parent.mkdir(exist_ok=True)
            out.write_text(self.settings.data_file.read_text())
            self.artifacts.copied = out

    with preserved_flow_registry():
        yield TProbeHook, seen


def test_a_launch_gives_the_flow_the_design_root_path_and_keeps_the_identity(
    hook_flow, tmp_path, monkeypatch
):
    """The flow reads the file of the design root, whatever the start directory is. The record of
    the settings keeps the text as written beside the path that the flow got. The run is reused
    at the next launch and the dry run calls it fresh: the identity is that of the text."""
    flow_class, seen = hook_flow
    root = tmp_path / "design"
    root.mkdir()
    (root / "hook.txt").write_text("from the design root\n")
    (tmp_path / "elsewhere").mkdir()
    (tmp_path / "elsewhere" / "hook.txt").write_text("from the start directory\n")
    monkeypatch.chdir(tmp_path / "elsewhere")
    design = Design(name="d", root=root, rtl={"sources": [], "top": "t"})
    given = {"data_file": "hook.txt"}
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    flow = runner.launch_flow(flow_class, design, given)
    assert flow.succeeded and seen == [design.root / "hook.txt"]
    assert Path(flow.artifacts.copied).read_text() == "from the design root\n"
    asked = flow_class.Settings.from_input(given, design_root=design.root, runner_cwd=tmp_path)
    assert flow.settings_hash == flow_run_hash(flow_class.name, asked, design.name)
    recorded = json.loads((flow.run_path / "settings.json").read_text())
    assert recorded["flow_settings"]["data_file"] == "hook.txt"
    assert recorded["effective_flow_settings"]["data_file"] == str(design.root / "hook.txt")
    again = DefaultRunner(tmp_path / "run", display_results=False)
    assert again.launch_flow(flow_class, design, given).reused
    _plan, states = again.plan_with_freshness(
        flow_class, design, flow_settings=node_settings(flow_class, given)
    )
    assert [state.state for state in states.values()] == ["fresh"]
