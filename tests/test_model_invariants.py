"""Invariants that must hold for *every* settings model, not just the ones with a bug history.

The pydantic v2 migration broke these one model at a time, and each break was found by hand.
Sweeping every registered flow instead means a newly added flow -- or a validator that gains a
normalizing step -- cannot reintroduce the same class of bug unnoticed.
"""

from pathlib import Path

import pytest

from xeda.design import Design, SourceType
from xeda.flow.flow import registered_flows
from xeda.flows import __builtin_flows__
from xeda.tool import Tool
from xeda.utils import semantic_hash

EXAMPLES_DIR = Path(__file__).parent.parent / "examples"

assert __builtin_flows__, "importing `xeda.flows` is what populates `registered_flows`"

#: Minimal settings that let a flow's `Settings` be constructed at all. A flow whose settings
#: still cannot be built from these is skipped rather than failed: requiring, say, a full
#: `VivadoSynth.Settings` for `vivado_postsynth_sim` is a property of that flow, not a defect.
MINIMAL_SETTINGS = {
    "fpga": {"part": "xc7a100tcsg324-1"},
    "clock_period": 5.0,
    "platform": "asap7",
    "target_libraries": ["nangate45.lib"],
}

#: Fields whose validator deliberately normalizes a value that `model_dump()` writes back out,
#: so re-validating changes it. Each entry must name why it is not a defect.
IDEMPOTENCE_EXCEPTIONS = {
    # `syn_cmdline_args` carries `validate_default=False` to mirror v1's lack of `always=True`:
    # the default `None` is left alone, but an explicitly supplied `None` becomes `[]`.
    ("DiamondSynth", "syn_cmdline_args"),
}


def _flow_classes():
    classes = {}
    for _, (_, cls) in sorted(registered_flows.items()):
        classes.setdefault(cls, cls.name)
    return sorted(classes.items(), key=lambda kv: kv[1])


def _settings_or_skip(cls):
    kwargs = {k: v for k, v in MINIMAL_SETTINGS.items() if k in cls.Settings.model_fields}
    try:
        return cls.Settings(**kwargs)
    except Exception as e:  # any failure here means "cannot build cheaply"
        pytest.skip(f"{cls.name}.Settings needs more than the minimal settings: {e}")


FLOWS = _flow_classes()
FLOW_IDS = [name for _, name in FLOWS]


@pytest.mark.parametrize("cls", [cls for cls, _ in FLOWS], ids=FLOW_IDS)
def test_settings_json_schema_builds(cls):
    """`xeda list-settings`, `--json` and the agent skill all go through this."""
    schema = cls.Settings.model_json_schema(by_alias=True)
    assert schema.get("properties"), f"{cls.name}: schema has no properties"


@pytest.mark.parametrize("cls", [cls for cls, _ in FLOWS], ids=FLOW_IDS)
def test_assigning_a_field_its_own_value_changes_nothing(cls):
    """Validators must be idempotent, or a settings round trip corrupts what it re-reads.

    `validate_assignment` re-runs a field's `mode="before"` validator on assignment, and so does
    reloading a `settings.json`. A validator that *transforms* rather than *normalizes* -- ISE's
    option quoting turned `"High"` into `""High""` -- therefore drifts a little further every
    time, which is invisible until a flow is re-run from its own recorded settings.
    """
    settings = _settings_or_skip(cls)
    for name in cls.Settings.model_fields:
        if name.endswith("_") or (cls.__name__, name) in IDEMPOTENCE_EXCEPTIONS:
            continue
        before = settings.model_dump()
        setattr(settings, name, getattr(settings, name))
        after = settings.model_dump()
        changed = {
            k: (before.get(k), after.get(k)) for k in before if before.get(k) != after.get(k)
        }
        assert not changed, f"{cls.name}: re-assigning {name!r} changed {changed}"


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
