"""Invariants that must hold for *every* settings model, not just the ones with a bug history.

Each sweep covers every registered flow and, where it takes values, every setting paired with
every structural kind of value (`settings_samples.PROBES`), so a newly added flow, setting or
validator cannot reintroduce a class of bug unnoticed.
"""

import copy
import json
from pathlib import Path

import pytest

from xeda.dataclass import ValidationError
from xeda.design import Design, SourceType
from xeda.flow import FlowSettingsError
from xeda.tool import Tool
from xeda.utils import dump_json, semantic_hash

from .settings_samples import PROBES, flow_classes, minimal_settings

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"

FLOWS = flow_classes()
FLOW_IDS = [name for _, name in FLOWS]


def _settings(cls):
    return cls.Settings(**minimal_settings(cls))


def _outcome(build):
    """`("accepted", settings dump)` or `("rejected", None)`; anything else propagates."""
    try:
        return "accepted", build().model_dump()
    except (ValidationError, FlowSettingsError):
        return "rejected", None


def _settings_fields(cls):
    return [name for name in cls.Settings.model_fields if not name.endswith("_")]


@pytest.mark.parametrize("cls", [cls for cls, _ in FLOWS], ids=FLOW_IDS)
def test_settings_json_schema_builds(cls):
    """`xeda list-settings`, `--json` and the agent skill all go through this."""
    schema = cls.Settings.model_json_schema(by_alias=True)
    assert schema.get("properties"), f"{cls.name}: schema has no properties"


@pytest.mark.parametrize("cls", [cls for cls, _ in FLOWS], ids=FLOW_IDS)
def test_a_setting_every_flow_shares_has_the_same_type_in_every_flow(cls):
    """`verbose` is a verbosity level everywhere; `nextpnr` and `open_xc7` once redeclared it as a
    `bool`, so `-s verbose=2` worked for every flow but those two."""
    from xeda.flow import Flow

    common = Flow.Settings.model_fields
    retyped = {
        name: (common[name].annotation, info.annotation)
        for name, info in cls.Settings.model_fields.items()
        if name in common and info.annotation != common[name].annotation
    }
    assert not retyped, f"{cls.name} changes the type of shared settings: {retyped}"


@pytest.mark.parametrize("cls", [cls for cls, _ in FLOWS], ids=FLOW_IDS)
def test_assigning_a_field_its_own_value_changes_nothing(cls):
    """Validators must be idempotent, or a settings round trip corrupts what it re-reads.

    `validate_assignment` re-runs a field's `mode="before"` validator on assignment, and so does
    reloading a `settings.json`. A validator that *transforms* rather than *normalizes* -- ISE's
    option quoting turned `"High"` into `""High""` -- therefore drifts a little further every
    time, which is invisible until a flow is re-run from its own recorded settings.
    """
    settings = _settings(cls)
    for name in cls.Settings.model_fields:
        if name.endswith("_"):
            continue
        before = settings.model_dump()
        setattr(settings, name, getattr(settings, name))
        after = settings.model_dump()
        changed = {
            k: (before.get(k), after.get(k)) for k in before if before.get(k) != after.get(k)
        }
        assert not changed, f"{cls.name}: re-assigning {name!r} changed {changed}"


@pytest.mark.parametrize("cls", [cls for cls, _ in FLOWS], ids=FLOW_IDS)
def test_assigning_a_value_is_the_same_as_constructing_with_it(cls):
    """Assigning any value to valid settings accepts or rejects it exactly as constructing with it
    does, and leaves exactly the same settings behind.

    The base leaves out the compatibility-only `clock_period` input where it is optional, and
    canonical `clock`/`clocks` values must not be combined with it at construction.
    """
    base = minimal_settings(cls, clock_period=False)
    mismatches = []
    for name in _settings_fields(cls):
        for value in PROBES:
            constructed = _outcome(lambda: cls.Settings(**{**base, name: copy.deepcopy(value)}))

            def assign():
                settings = cls.Settings(**base)
                setattr(settings, name, copy.deepcopy(value))
                return settings

            assigned = _outcome(assign)
            if constructed[0] != assigned[0]:
                mismatches.append(
                    f"{name}={value!r}: {constructed[0]} vs {assigned[0]} on assignment"
                )
            elif constructed != assigned:
                differ = [k for k in constructed[1] if constructed[1][k] != assigned[1][k]]
                mismatches.append(f"{name}={value!r}: assignment differs in {differ}")
    assert not mismatches, f"{cls.name}:\n  " + "\n  ".join(mismatches)


@pytest.mark.parametrize("cls", [cls for cls, _ in FLOWS], ids=FLOW_IDS)
def test_settings_reload_unchanged_from_their_settings_json(cls, tmp_path):
    """What a run writes to `settings.json` must rebuild the same settings, for every accepted
    value of every setting -- the remote runner, for one, relaunches a flow from it."""
    base = minimal_settings(cls)
    changed = []
    for name in _settings_fields(cls):
        for value in PROBES:
            try:
                settings = cls.Settings(**{**base, name: copy.deepcopy(value)})
            except (ValidationError, FlowSettingsError):
                continue
            dump_json({"flow_settings": settings}, tmp_path / "settings.json", backup=False)
            saved = json.loads((tmp_path / "settings.json").read_text())["flow_settings"]
            try:
                reloaded = cls.Settings(**saved)
            except (ValidationError, FlowSettingsError) as e:
                changed.append(f"{name}={value!r}: its settings.json does not load: {e}")
                continue
            if reloaded.model_dump() != settings.model_dump():
                differ = [
                    k for k, v in settings.model_dump().items() if reloaded.model_dump()[k] != v
                ]
                changed.append(f"{name}={value!r}: reloading changes {differ}")
    assert not changed, f"{cls.name}:\n  " + "\n  ".join(changed)


# ---------------------------------------------------------------------------------------------
# `semantic_hash` decides which run directory a flow lands in and whether a cached dependency is
# reused, so anything that perturbs it silently changes where results are written.
# ---------------------------------------------------------------------------------------------


def test_semantic_hash_ignores_cached_property_caches():
    """`cached_property` writes into a model's `__dict__`, which is where its fields live too."""
    tool = Tool(executable="python3")  # type: ignore[call-arg]
    before = semantic_hash(tool)

    _ = tool.info  # populates `version`, `version_str`, ... in `tool.__dict__`

    assert semantic_hash(tool) == before


def test_semantic_hash_of_an_enum_terminates():
    """An enum member's `__dict__` links back to its class, whose `__dict__` lists every member."""
    assert semantic_hash(SourceType.Vhdl) != semantic_hash(SourceType.Verilog)


def test_semantic_hash_of_a_whole_design_terminates():
    """`DesignSource.type` is a `SourceType`, so hashing a design used to recurse until it died."""
    design = Design.from_toml(EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.toml")

    assert semantic_hash(design) == semantic_hash(
        Design.from_toml(EXAMPLES_DIR / "vhdl" / "sqrt" / "sqrt.toml")
    )


# ---------------------------------------------------------------------------------------------
# Copies that drop their cached values: `Tool.derive`, the dependency settings a flow is handed.
# ---------------------------------------------------------------------------------------------


def _all_model_classes():
    import xeda.flow_runner.dse  # noqa: F401 - registers the optimizer models
    import xeda.flows  # noqa: F401 - registers every flow, tool and platform model
    from xeda.dataclass import XedaBaseModel

    seen, pending = set(), [XedaBaseModel]
    while pending:
        cls = pending.pop()
        if cls not in seen:
            seen.add(cls)
            pending.extend(cls.__subclasses__())
    return sorted(seen, key=lambda cls: f"{cls.__module__}.{cls.__qualname__}")


def test_invalidating_cached_properties_clears_the_inherited_ones_too():
    """A `cached_property` caches in the instance `__dict__` whichever class declares it; walking
    only the instance's own class left `VivadoTool` holding `Tool.version` after `derive()`."""
    from functools import cached_property

    stale = {}
    for cls in _all_model_classes():
        names = {
            name
            for klass in cls.__mro__
            for name, attr in vars(klass).items()
            if isinstance(attr, cached_property)
        }
        if not names:
            continue
        model = cls.model_construct()
        for name in names:
            model.__dict__[name] = "stale"
        model.invalidate_cached_properties()
        if left := sorted(names & model.__dict__.keys()):
            stale[f"{cls.__module__}.{cls.__qualname__}"] = left
    assert not stale, f"cached values survive invalidation: {stale}"


def test_a_tool_derived_from_a_tool_subclass_does_not_keep_its_version():
    from xeda.flows.vivado import VivadoTool

    vivado = VivadoTool()  # type: ignore[call-arg]
    vivado.__dict__.update(version=("2023", "2"), version_output="Vivado v2023.2")
    derived = vivado.derive("hw_server")
    assert "version" not in derived.__dict__
    assert "version_output" not in derived.__dict__
    assert vivado.version == ("2023", "2")  # the source keeps its own cache


# ---------------------------------------------------------------------------------------------
# The order settings are given in never matters
# ---------------------------------------------------------------------------------------------


def _field_validators_reading_other_settings(settings_cls):
    import inspect

    return sorted(
        name
        for name, decorator in settings_cls.__pydantic_decorators__.field_validators.items()
        if "info.data" in inspect.getsource(decorator.func)
    )


@pytest.mark.parametrize("cls", [cls for cls, _ in FLOWS], ids=FLOW_IDS)
def test_no_field_validator_reads_another_setting(cls):
    """A field validator sees the other settings (`info.data`) only when its *own* field is
    validated, so its result depends on the order settings were given in: `quiet` was reset by a
    `verbose` given with it, but not by one assigned after it. Whatever depends on several
    settings is decided where it is read (`Flow.Settings.is_quiet`)."""
    assert not _field_validators_reading_other_settings(cls.Settings)


def test_quiet_verbose_and_debug_mean_the_same_whatever_order_they_are_given_in():
    from xeda.flows import YosysFpga

    Settings = YosysFpga.Settings
    for louder in ({"verbose": 2}, {"debug": True}):
        constructed = Settings(quiet=True, **louder)
        assigned_after = Settings(quiet=True)
        assigned_before = Settings(**louder)
        for name, value in louder.items():
            setattr(assigned_after, name, value)
        assigned_before.quiet = True
        settings = [constructed, assigned_after, assigned_before]
        assert all(s.model_dump() == constructed.model_dump() for s in settings), louder
        assert not any(s.is_quiet for s in settings), louder
    assert Settings(quiet=True).is_quiet
