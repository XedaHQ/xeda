"""Settings a flow shares with the dependencies it launches (`Flow.Settings.dependency_settings`).

The contract, checked for every declared (flow, dependency, shared setting):

1. Settings hold what was written. Neither construction nor assignment copies anything between a
   flow's settings and its dependency's, so the outcome never depends on the order settings were
   given in, and `settings.json` reloads unchanged.
2. `resolve_dependency` -- which the flow's `init` passes to `add_dependency` -- takes each shared
   setting from the flow unless it is unset there (`None` or empty), from the dependency
   otherwise, and the flow adopts the same value, so both run with it.
3. The resolved settings are a private copy: resolving leaves the given dependency settings as
   they were and shares no object with the flow.

Everything here is driven by the declarations, so a new dependency flow is covered automatically.
"""

from pathlib import Path
from typing import Any, Dict, Tuple

import pytest

from xeda.design import Design
from xeda.flow import Flow, is_unset
from xeda.flows.yosys.common import YosysBase

from .settings_samples import flow_classes, minimal_settings

CASES = [
    (cls, field, name)
    for cls, _ in flow_classes()
    for field, names in cls.Settings.dependency_settings.items()
    for name in names
]


def _ids(cases):
    return [f"{cls.name}.{field}.{name}" for cls, field, name in cases]


CASE_IDS = _ids(CASES)


def _default(cls, name):
    return cls.Settings.model_fields[name].get_default(call_default_factory=True)


#: Shared settings the flow leaves unset by default, and those whose default is a value.
UNSET_BY_DEFAULT = [case for case in CASES if is_unset(_default(case[0], case[2]))]
VALUE_BY_DEFAULT = [case for case in CASES if not is_unset(_default(case[0], case[2]))]
DEPENDENCIES = sorted({(cls, field) for cls, field, _ in CASES}, key=lambda c: (c[0].name, c[1]))
DEPENDENCY_IDS = [f"{cls.name}.{field}" for cls, field in DEPENDENCIES]

_ECP5 = ({"part": "LFE5U-45F-6BG381C"}, {"part": "LFE5U-25F-6BG381C"})

#: Two different valid values of each shared setting: (the flow's, the dependency's).
SAMPLES: Dict[str, Tuple[Any, Any]] = {
    "fpga": _ECP5,
    "clocks": (
        {"sys": {"freq": "25MHz"}},
        {"clk_a": {"freq": "100MHz"}, "clk_b": {"freq": "50MHz"}},
    ),
    "board": ("ulx3s", "custom_board"),
    "custom_boards_file": (
        Path(__file__).parent / "resources" / "boards_a.toml",
        Path(__file__).parent / "resources" / "boards_b.toml",
    ),
    "timing_sim": (True, False),
    "elab_debug": ("all", "off"),
    "saif": ("flow.saif", "dependency.saif"),
}
SAMPLES_BY_FLOW = {
    ("open_xc7", "fpga"): ({"part": "xc7a100tftg256-2L"}, {"part": "xc7a35tcpg236-1"})
}

#: Nested flow settings that are options, not the settings of a dependency the flow launches.
NOT_DEPENDENCIES = {
    (YosysBase.Settings, "ghdl"): "options for the ghdl-yosys plugin that reads VHDL sources",
}


def _samples(cls, name):
    return SAMPLES_BY_FLOW.get((cls.name, name), SAMPLES[name])


def _settings(cls, field, **kwargs):
    base = minimal_settings(cls, clock_period=False)
    for key in list(base):
        if key in kwargs or key in cls.Settings.dependency_settings.get(field, ()):
            base.pop(key)  # the test decides every shared setting itself
    return cls.Settings(**{**base, **kwargs})


def _dependency_input(cls, field, **shared):
    """The dependency's own minimal settings plus `shared`, as given inside the flow's settings."""
    given = dict(minimal_settings(cls).get(field, {}))
    given.update(shared)
    return given


def _value(cls, name, given):
    """`given` as the flow's settings hold it once validated (a mapping becomes a model, ...)."""
    return getattr(cls.Settings(**{**minimal_settings(cls, clock_period=False), name: given}), name)


# ---------------------------------------------------------------------------------------------
# The declarations themselves
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("cls", "field", "name"), CASES, ids=CASE_IDS)
def test_a_shared_setting_is_a_setting_of_both_flows(cls, field, name):
    dependency_cls = cls.Settings.model_fields[field].annotation
    assert isinstance(dependency_cls, type) and issubclass(dependency_cls, Flow.Settings)
    assert name in cls.Settings.model_fields, f"{cls.name} has no setting {name!r}"
    assert name in dependency_cls.model_fields, f"{field} has no setting {name!r}"
    assert name in SAMPLES, f"add two sample values of {name!r} to SAMPLES"


def test_every_nested_flow_settings_field_is_a_declared_dependency():
    """So no dependency silently misses sharing, copying and resolution."""
    undeclared = []
    for cls, _ in flow_classes():
        for field, info in cls.Settings.model_fields.items():
            nested = info.annotation
            if not (isinstance(nested, type) and issubclass(nested, Flow.Settings)):
                continue
            declared_in = [c for c in cls.Settings.__mro__ if (c, field) in NOT_DEPENDENCIES]
            if field not in cls.Settings.dependency_settings and not declared_in:
                undeclared.append(f"{cls.name}.{field}")
    assert not undeclared, f"declare these in `dependency_settings`: {undeclared}"


@pytest.mark.parametrize(("cls", "field"), DEPENDENCIES, ids=DEPENDENCY_IDS)
def test_init_launches_the_dependency_with_the_resolved_settings(cls, field, tmp_path):
    """The wiring: whatever `resolve_dependency` computes is what the dependency runs with."""
    design = Design(
        name="d",
        rtl={"sources": [], "top": "t", "clock_port": "clk"},
        tb={"sources": [], "top": "tb"},
        design_root=tmp_path,
    )
    flow = cls(_settings(cls, field, **minimal_settings(cls)), design, run_path=tmp_path)
    expected = flow.settings.resolve_dependency(field)

    flow.init()

    launched = [settings for _, settings, _ in flow.dependencies]
    assert len(launched) == 1
    assert launched[0].model_dump() == expected.model_dump()
    assert launched[0] is not getattr(flow.settings, field)


# ---------------------------------------------------------------------------------------------
# Settings hold what was written
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("cls", "field", "name"), CASES, ids=CASE_IDS)
def test_construction_and_assignment_copy_nothing_between_the_flows(cls, field, name):
    ours, theirs = _samples(cls, name)
    settings = _settings(
        cls, field, **{name: ours, field: _dependency_input(cls, field, **{name: theirs})}
    )

    assert getattr(settings, name) == _value(cls, name, ours)
    assert getattr(getattr(settings, field), name) == _value(cls, name, theirs)

    setattr(settings, name, theirs)
    assert getattr(getattr(settings, field), name) == _value(cls, name, theirs)
    setattr(settings, name, ours)
    assert getattr(getattr(settings, field), name) == _value(cls, name, theirs), "order mattered"


@pytest.mark.parametrize(("cls", "field"), DEPENDENCIES, ids=DEPENDENCY_IDS)
def test_a_given_settings_instance_is_copied_not_shared(cls, field):
    """A caller's settings object for the dependency is the caller's: the flow keeps its own copy,
    and neither assigning nor resolving can reach back into it."""
    dependency_cls = cls.Settings.model_fields[field].annotation
    theirs = dependency_cls(**_dependency_input(cls, field))
    before = theirs.model_dump()
    settings = _settings(cls, field, **{field: theirs})

    assert getattr(settings, field) is not theirs
    setattr(settings, field, theirs)
    assert getattr(settings, field) is not theirs, "assigning must copy it too"
    for name in cls.Settings.dependency_settings[field]:
        setattr(settings, name, _samples(cls, name)[0])
    settings.resolve_dependency(field)
    getattr(settings, field).verbose = 3
    assert theirs.model_dump() == before


# ---------------------------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(("cls", "field", "name"), CASES, ids=CASE_IDS)
def test_the_flows_value_wins_when_it_is_set(cls, field, name):
    ours, theirs = _samples(cls, name)
    settings = _settings(
        cls, field, **{name: ours, field: _dependency_input(cls, field, **{name: theirs})}
    )

    resolved = settings.resolve_dependency(field)

    assert getattr(resolved, name) == _value(cls, name, ours)
    assert getattr(settings, name) == _value(cls, name, ours)


@pytest.mark.parametrize(("cls", "field", "name"), UNSET_BY_DEFAULT, ids=_ids(UNSET_BY_DEFAULT))
def test_the_dependencys_value_is_used_and_adopted_when_the_flow_leaves_it_unset(cls, field, name):
    _, theirs = _samples(cls, name)
    settings = _settings(cls, field, **{field: _dependency_input(cls, field, **{name: theirs})})

    resolved = settings.resolve_dependency(field)

    assert getattr(resolved, name) == _value(cls, name, theirs)
    assert getattr(settings, name) == _value(cls, name, theirs), "the flow must adopt it"


@pytest.mark.parametrize(("cls", "field", "name"), VALUE_BY_DEFAULT, ids=_ids(VALUE_BY_DEFAULT))
def test_a_flow_default_that_is_a_value_applies_to_the_dependency(cls, field, name):
    default = _default(cls, name)
    _, theirs = _samples(cls, name)
    assert theirs != default, "the dependency's sample must differ from the flow's default"
    settings = _settings(cls, field, **{field: _dependency_input(cls, field, **{name: theirs})})

    assert getattr(settings.resolve_dependency(field), name) == default


@pytest.mark.parametrize(("cls", "field", "name"), CASES, ids=CASE_IDS)
def test_resolving_is_a_private_copy_that_leaves_the_given_settings_alone(cls, field, name):
    ours, theirs = _samples(cls, name)
    settings = _settings(
        cls, field, **{name: ours, field: _dependency_input(cls, field, **{name: theirs})}
    )
    given = getattr(settings, field).model_dump()

    resolved = settings.resolve_dependency(field)

    assert getattr(settings, field).model_dump() == given
    assert resolved is not getattr(settings, field)
    value = getattr(resolved, name)
    if isinstance(value, (dict, list)) or hasattr(value, "model_dump"):
        assert value is not getattr(settings, name)


@pytest.mark.parametrize(("cls", "field"), DEPENDENCIES, ids=DEPENDENCY_IDS)
def test_resolution_is_stable_across_a_settings_json_round_trip(cls, field):
    names = cls.Settings.dependency_settings[field]
    given = {name: _samples(cls, name)[1] for name in names}
    settings = _settings(cls, field, **{field: _dependency_input(cls, field, **given)})

    reloaded = cls.Settings.model_validate(settings.model_dump())

    assert reloaded.model_dump() == settings.model_dump()
    assert (
        reloaded.resolve_dependency(field).model_dump()
        == settings.resolve_dependency(field).model_dump()
    )


# ---------------------------------------------------------------------------------------------
# Scenarios behind the rules
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize("flow", ["nextpnr", "open_xc7"])
def test_a_multi_clock_dependency_is_kept_when_the_flow_gives_no_clocks(flow):
    """An empty `clocks` means "not given"; letting it replace the dependency's clocks made
    yosys re-derive one `main_clock` from its `clock_period`, dropping `clk_b`."""
    from xeda.flow_runner import get_flow_class

    cls = get_flow_class(flow)
    clocks = {"clk_a": {"freq": "100MHz"}, "clk_b": {"freq": "50MHz"}}
    for flow_clocks in ({}, None):
        kwargs = {} if flow_clocks is None else {"clocks": flow_clocks}
        settings = _settings(cls, "yosys", yosys={"clocks": clocks}, **kwargs)

        resolved = settings.resolve_dependency("yosys")

        assert {k: c.freq for k, c in resolved.clocks.items()} == {"clk_a": 100.0, "clk_b": 50.0}


@pytest.mark.parametrize("flow", ["nextpnr", "open_xc7"])
def test_the_resolved_clock_keeps_canonical_clock_in_step(flow):
    """A canonical clock is propagated to the dependency without a second stored spelling."""
    from xeda.flow_runner import get_flow_class

    cls = get_flow_class(flow)
    settings = _settings(cls, "yosys", clock={"period": 5.0}, yosys={"clock": {"period": 10.0}})

    resolved = settings.resolve_dependency("yosys")

    assert resolved.main_clock is not None and resolved.main_clock.period == 5.0
    assert resolved.clock_period == 5.0


def test_openfpgaloader_resolves_its_whole_chain_consistently():
    """`openfpgaloader` -> `nextpnr` -> `yosys_fpga`, each resolved when it is launched."""
    from xeda.flows.openfpgaloader import Openfpgaloader

    settings = Openfpgaloader.Settings(
        clock_period=10.0, nextpnr={"yosys": {"fpga": {"part": "LFE5U-25F-6BG381C"}}}
    )

    nextpnr = settings.resolve_dependency("nextpnr")
    yosys = nextpnr.resolve_dependency("yosys")

    assert settings.fpga is None, "the part is given only inside yosys' settings"
    assert yosys.fpga is not None and yosys.fpga.part == "LFE5U-25F-6BG381C"
    assert nextpnr.fpga is not None and nextpnr.fpga.part == "LFE5U-25F-6BG381C"
    assert yosys.clock_period == nextpnr.clock_period == 10.0
