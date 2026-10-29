"""`package_inputs` ships a flow section whole.

The sections of a request go to the remote host in a design document that the remote plans again.
These sweeps give every flow a section that holds each key the validation of a section accepts,
and compare what the remote reads with what this side read: the keys, the settings, and for the
bindings the edges of the plan. The keys come from the code that validates a section (the fields
of `Flow.Settings`, its removed settings, and the reserved key of the bindings), and the bindings
come from the declared inputs and outputs of the flows. A new key of a section has to ship, or
these tests fail.
"""

import json
from collections.abc import Sequence
from pathlib import Path, PurePath, PurePosixPath
from typing import Any, Iterator

import pytest
from pydantic import BaseModel

from xeda.dataclass import is_locator, written_role
from xeda.flow import Flow, FlowSettingsError, FlowSettingsException
from xeda.flow.flow import registered_flows
from xeda.flow.io import declared_inputs, declared_outputs
from xeda.flow_runner.bindings import INPUTS_KEY, BindingEntry, BindingLayer, NodeKey
from xeda.remote.packaging import package_inputs

from . import io_flows
from .flow_registry import is_product_flow
from .settings_samples import PROBES, flow_classes, minimal_settings
from .test_remote_packaging import (
    REMOTE_INPUTS,
    _design,
    _package,
    _remote_plan,
    _shipped,
    _sync,
    _wiring,
)

pytestmark = pytest.mark.python_compat


def _test_flows() -> list[type[Flow]]:
    """The flows that `tests.io_flows` declares, once each."""
    found = {
        cls
        for _name, (module, cls) in registered_flows.items()
        if module == io_flows.__name__ and not is_product_flow(cls)
    }
    return sorted(found, key=lambda cls: cls.name)


def _all_flows() -> list[type[Flow]]:
    return [cls for cls, _name in flow_classes()] + _test_flows()


FLOWS = _all_flows()
FLOW_IDS = [cls.name for cls in FLOWS]


def _leaves(model: type[BaseModel], prefix: tuple[str, ...] = ()) -> Iterator[tuple[str, ...]]:
    """The paths of the settings that a section can state: each field, and inside a field that
    holds a group of settings, each field of the group."""
    for name, info in model.model_fields.items():
        annotation = info.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            yield from _leaves(annotation, (*prefix, name))
        else:
            yield (*prefix, name)


def _owner(model: type[BaseModel], path: tuple[str, ...]) -> tuple[type[BaseModel], str]:
    """The class that holds the setting at `path`, and its name there."""
    for name in path[:-1]:
        model = model.model_fields[name].annotation  # type: ignore[assignment]
    return model, path[-1]


def _is_written(model: type[BaseModel], path: tuple[str, ...]) -> bool:
    """Whether the setting at `path` names a file that the run writes (it has a written role)."""
    owner, name = _owner(model, path)
    return written_role(owner, name) is not None


def _holds_path(value: Any) -> bool:
    """Whether `value` is a path, or a collection that holds one."""
    if isinstance(value, PurePath):
        return True
    if isinstance(value, dict):
        value = list(value.values())
    return isinstance(value, (list, tuple)) and any(_holds_path(item) for item in value)


def _at(path: tuple[str, ...], value: Any) -> dict[str, Any]:
    for name in reversed(path):
        value = {name: value}
    return value


def _merge(base: dict[str, Any], more: dict[str, Any]) -> dict[str, Any]:
    result = dict(base)
    for key, value in more.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _merge(result[key], value)
        else:
            result[key] = value
    return result


def _default_at(settings: BaseModel, path: tuple[str, ...]) -> Any:
    value: Any = settings
    for name in path:
        value = getattr(value, name)
    return json.loads(json.dumps(value, default=_plain))


def _plain(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    return str(value)


#: The settings for which neither the default nor any probe is a valid input, with the reason.
#: The sweep below fails for any other setting that it cannot cover.
UNCOVERED = {
    ("clocks",): (
        "the input spelling is the shorthand `clock`, which `minimal_settings` states; every "
        "section of the sweep holds the stored `clocks` it makes"
    ),
}


def _sections(
    cls: type[Flow],
    design_root: Path,
    skipped: list[tuple[str, ...]],
    files: Sequence[Path] = (),
) -> Iterator[tuple[tuple[str, ...], dict]]:
    """Yield `(path, section)` for each setting of `cls` that has a value its validation takes.

    The value is the default of the setting, set on purpose, and then the first of the probes
    that the validation accepts. So an enumerated setting is covered too. A setting with no such
    value is appended to `skipped`. A setting that holds a path also gets each of `files` (a
    file below the design root, and one outside it), so the sweep compares a path that travels."""
    base = minimal_settings(cls)
    try:
        defaults = cls.Settings.from_input(base, design_root=design_root, runner_cwd=design_root)
    except FlowSettingsError:
        return
    for path in _leaves(cls.Settings):
        candidates = [_default_at(defaults, path), *PROBES]
        for candidate in candidates:
            section = _merge(base, _at(path, candidate))
            try:
                cls.Settings.from_input(section, design_root=design_root, runner_cwd=design_root)
            except (FlowSettingsError, ValueError, TypeError):
                continue
            yield path, section
            break
        else:
            skipped.append(path)
            continue
        for file in files:
            section = _merge(base, _at(path, str(file)))
            try:
                model = cls.Settings.from_input(
                    section, design_root=design_root, runner_cwd=design_root
                )
            except (FlowSettingsError, ValueError, TypeError):
                continue
            value: Any = model
            for name in path:
                value = getattr(value, name)
            # a locator names a place on the machine that reads it, and does not travel
            if _holds_path(value) and not is_locator(*_owner(cls.Settings, path)):
                yield path, section


def _compared(text: str, stripped: list[str]) -> str:
    """Return `text` (the JSON of a setting) with each place in `stripped` removed from the
    paths that start with it, and the `$DESIGN_ROOT` spelling removed too. What is left names a
    path by its place below the design root."""
    for place in stripped:
        text = text.replace(f"{place}/", "")
    return text.replace("$DESIGN_ROOT/", "")


@pytest.mark.parametrize("cls", FLOWS, ids=FLOW_IDS)
def test_every_setting_of_a_section_reaches_the_remote(tmp_path, cls):
    design = _design(tmp_path)
    root = design.root
    remote_root = tmp_path / "remote_inputs"
    skipped: list[tuple[str, ...]] = []
    inside = root / "inside.dat"
    outside = tmp_path / "elsewhere" / "outside.dat"
    outside.parent.mkdir()
    for file in (inside, outside):
        file.write_text("data\n")
    checked = 0
    for path, section in _sections(cls, root, skipped, (inside, outside)):
        local = cls.Settings.from_input(section, design_root=root, runner_cwd=root)
        package = package_inputs(
            design,
            {cls.name: section},
            {cls.name},
            run_root=tmp_path / "xeda_run",
            remote_inputs=PurePosixPath(remote_root),
        )
        _sync(package, remote_root)
        shipped = _shipped(package)["flows"][cls.name]
        where = f"{cls.name}: {'.'.join(path)}"
        # the section keeps its keys: none is lost, and none is new
        assert set(shipped) == local.model_fields_set, where
        remote = cls.Settings.from_input(shipped, design_root=remote_root, runner_cwd=remote_root)
        assert remote.model_fields_set == local.model_fields_set, where
        # A path names the same place on both sides: below the design root here and below the
        # inputs directory there, or elsewhere here and below `_setting_inputs` there, where
        # `path_identities` gives the name that this side counts for it.
        identities = sorted(package.path_identities.items(), key=lambda item: -len(item[0]))
        for key in local.model_fields_set:
            before = json.dumps(getattr(local, key), default=_plain, sort_keys=True)
            after = json.dumps(getattr(remote, key), default=_plain, sort_keys=True)
            for place, roles in identities:
                if "setting" in roles:
                    after = after.replace(place, roles["setting"])
            here = sorted({str(root), str(root.resolve())}, key=len, reverse=True)
            # A setting that names a file the run writes keeps its text: the remote writes below
            # its own run directory, and the identity counts the name.
            there = here if key == path[0] and _is_written(cls.Settings, path) else []
            assert _compared(after, [str(remote_root), *there]) == _compared(
                before, here
            ), f"{where}: {key}"
        checked += 1
    assert set(skipped) <= UNCOVERED.keys(), f"{cls.name}: no value for {sorted(set(skipped))}"
    assert checked or not cls.Settings.model_fields, f"nothing of {cls.name} was checked"


@pytest.mark.parametrize("cls", FLOWS, ids=FLOW_IDS)
def test_a_removed_setting_is_still_refused_on_the_remote(tmp_path, cls):
    removed = cls.Settings.removed_setting_replacements()
    design = _design(tmp_path)
    root = design.root
    for name in removed:
        section = {name: "x"}
        with pytest.raises(FlowSettingsError) as local:
            cls.Settings.from_input(section, design_root=root, runner_cwd=root)
        # the flow is not in the plan, so the request had no reason to refuse it here
        package = package_unplanned(design, cls.name, section, tmp_path)
        shipped = _shipped(package)["flows"][cls.name]
        assert shipped == section
        with pytest.raises(FlowSettingsError) as remote:
            cls.Settings.from_input(shipped, design_root=root, runner_cwd=root)
        assert str(remote.value) == str(local.value)


def package_unplanned(design, flow: str, section: dict, tmp_path: Path):
    from pathlib import PurePosixPath

    from xeda.remote.packaging import package_inputs

    return package_inputs(
        design,
        {flow: section},
        set(),
        run_root=tmp_path / "xeda_run",
        remote_inputs=PurePosixPath(REMOTE_INPUTS),
    )


@pytest.mark.parametrize("cls", FLOWS, ids=FLOW_IDS)
def test_every_declared_input_of_a_flow_can_ship_its_binding(tmp_path, cls):
    inputs = [name for name, declared in declared_inputs(cls).items() if not declared.is_design]
    design = _design(tmp_path)
    layer = BindingLayer(
        "a test",
        tuple(
            BindingEntry(NodeKey(cls.name), name, f"p{index}.out", False, "a test")
            for index, name in enumerate(inputs)
        ),
        {},
    )
    package = _package(design, {}, tmp_path, bindings=(layer,))
    shipped = _shipped(package)["flows"]
    if inputs:
        assert shipped[cls.name][INPUTS_KEY] == {
            name: f"p{index}.out" for index, name in enumerate(inputs)
        }
    else:
        assert cls.name not in shipped


# ----------------------------------------------------------------- the edges of every binding


def _bindings() -> list[tuple[str, str, str, str]]:
    """Every `(consumer, input, producer, output)` of the test flows: a binding that names an
    output of a test flow for an input of a test flow. The planner decides which ones are
    valid."""
    flows = _test_flows()
    return [
        (consumer.name, name, producer.name, output)
        for consumer in flows
        for name, declared in declared_inputs(consumer).items()
        if not declared.is_design
        for producer in flows
        for output in declared_outputs(producer)
    ]


BINDINGS = _bindings()
#: How many of `BINDINGS` the planner takes. Raise it when a test flow adds bindings.
ACCEPTED_BINDINGS = 381


def _planned(tmp_path: Path, consumer: str, name: str, producer: str, output: str):
    """Plan the consumer with the binding in the design file, or return None if the planner
    refuses the request."""
    import yaml

    from xeda.flow_runner import DefaultRunner

    root = tmp_path / "design"
    root.mkdir(parents=True, exist_ok=True)
    (root / "input.txt").write_text("from the bound producer\n")
    (root / "d.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "d",
                "rtl": {"sources": [], "top": "t"},
                "flows": {
                    consumer: {"inputs": {name: f"{producer}.{output}"}},
                    "t_input_maker": {"input_file": "input.txt"},
                },
            }
        )
    )
    local_root = tmp_path / "local" / "xeda_run"
    launcher = DefaultRunner(local_root, display_results=False)
    try:
        return launcher.prepare_request(consumer, design=root / "d.yaml"), local_root
    except FlowSettingsException:  # the planner refuses a binding that is not valid
        return None


def test_the_remote_plans_every_binding_that_the_local_plan_accepts(tmp_path):
    """Bind each input of each test flow to each output of each test flow. The planner takes
    some of them and refuses the rest. The remote plans each accepted one with the same edges
    and identities."""
    accepted = 0
    rebound = 0
    for index, (consumer, name, producer, output) in enumerate(BINDINGS):
        case = tmp_path / str(index)
        planned = _planned(case, consumer, name, producer, output)
        if planned is None:
            continue
        prepared, local_root = planned
        accepted += 1
        package, remote_plan, remote_root, _ = _remote_plan(
            case, prepared, local_root, consumer, hashed=False
        )
        assert _wiring(remote_plan, remote_root) == _wiring(prepared.plan, local_root), (
            consumer,
            name,
            producer,
            output,
        )
        default = declared_inputs(prepared.plan.node(consumer).flow_class)[name].producer
        if default != producer:
            rebound += 1
    # A planner bug is no refusal (only `FlowSettingsException` is caught), and a planner that
    # refuses more than it did shrinks the sweep: this is the number it takes today.
    assert accepted >= ACCEPTED_BINDINGS, f"the planner took {accepted} bindings"
    assert rebound, "every binding that the planner took names the default producer"
