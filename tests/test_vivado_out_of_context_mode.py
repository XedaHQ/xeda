"""`out_of_context` asks `synth_design` for `-mode out_of_context`.

The flows that add that mode (`vivado_synth`, `vivado_project`, `vivado_alt_synth`) keep one that
the design already gives, and refuse any other `-mode` given with it before anything runs: Vivado
would get two modes, and the settings would not say which one holds. Without `out_of_context`, a
`-mode` of the design's own goes through as written.
"""

import re

import pytest

from xeda.flow import FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda.flows import VivadoAltSynth, VivadoProject, VivadoSynth
from xeda.flows.vivado.vivado_synth import run_steps
from xeda.utils import WorkingDirectory

from .test_vivado_step_tables import PART, _design, _run, _script, needs_tclsh
from .tool_utils import use_fake_tools

SETTINGS = {"fpga": PART, "clock": {"period": 5.5}}

#: Where each flow takes the options of `synth_design`, and how its script shows them.
MORE_OPTIONS = "synth.steps.SYNTH_DESIGN.ARGS.MORE.OPTIONS"
SYNTH_STEP = "synth.steps.synth"


def more_options(options) -> dict:
    return {"synth": {"steps": {"SYNTH_DESIGN": {"ARGS": {"MORE": {"OPTIONS": options}}}}}}


def synth_step(options) -> dict:
    return {"synth": {"steps": {"synth": options}}}


#: flow, script, where the design writes the options, how it writes them, and what a script holds
PROJECT_MODE = [
    pytest.param(VivadoSynth, "vivado_synth.tcl", id="vivado_synth"),
    pytest.param(VivadoProject, "vivado_project.tcl", id="vivado_project"),
]


def project_mode_options(script: str) -> str:
    """The value the script gives `STEPS.SYNTH_DESIGN.ARGS.MORE OPTIONS` (the last one holds)."""
    values = re.findall(r'"STEPS\.SYNTH_DESIGN\.ARGS\.MORE OPTIONS" (?:-value )?"([^"]*)"', script)
    assert values, script
    return values[-1]


def synth_design_line(script: str) -> str:
    (line,) = [line for line in script.splitlines() if line.startswith("synth_design ")]
    return line


# ---------------------------------------------------------------- vivado_synth, vivado_project


@needs_tclsh
@pytest.mark.parametrize("flow_class, script", PROJECT_MODE)
@pytest.mark.parametrize(
    "options",
    [
        pytest.param(["-mode out_of_context"], id="list"),
        pytest.param("-mode out_of_context", id="text"),
        pytest.param(["-retiming", "-mode out_of_context"], id="among-others"),
        pytest.param(["-retiming -mode out_of_context -assert"], id="in-one-string"),
        # Vivado takes an abbreviation of the switch, and a value in either letter case
        pytest.param(["-mod out_of_context"], id="abbreviated"),
        pytest.param(["-mode OUT_OF_CONTEXT"], id="upper-case"),
    ],
)
def test_the_out_of_context_mode_the_design_gives_is_kept_once(
    flow_class, script, options, tmp_path, monkeypatch
) -> None:
    run = _run(
        flow_class,
        tmp_path / "run",
        _design(),
        {**SETTINGS, "out_of_context": True, **more_options(options)},
        monkeypatch,
    )
    value = project_mode_options(_script(run, script))
    # what the design wrote is there, in its order, and the flow added no mode to it
    assert value == (options if isinstance(options, str) else " ".join(options))
    assert value.lower().count("out_of_context") == 1, value


@pytest.mark.parametrize("flow_class, script", PROJECT_MODE)
@pytest.mark.parametrize(
    "options, shown",
    [
        pytest.param(["-mode default"], "-mode default", id="list"),
        pytest.param("-mode default", "-mode default", id="text"),
        pytest.param(["-retiming", "-mode default"], "-mode default", id="among-others"),
        pytest.param(["-retiming -mode default -assert"], "-mode default", id="in-one-string"),
        pytest.param(["-mode out_of_context", "-mode default"], "-mode default", id="both"),
        pytest.param(["-mod default"], "-mod default", id="abbreviated"),
        pytest.param(["-mo default"], "-mo default", id="shortest-abbreviation"),
        pytest.param(["-mode Default"], "-mode Default", id="capitalized"),
    ],
)
def test_another_mode_with_out_of_context_is_refused_before_anything_runs(
    flow_class, script, options, shown, tmp_path, monkeypatch
) -> None:
    use_fake_tools(monkeypatch)
    root = tmp_path / "run"
    with pytest.raises(FlowSettingsException) as error:
        DefaultRunner(root, display_results=False).run_flow(
            flow_class,
            _design(),
            {**SETTINGS, "out_of_context": True, **more_options(options)},
        )
    message = str(error.value)
    assert "out_of_context" in message and MORE_OPTIONS in message and shown in message
    assert not (root / "design0").exists(), "the flow had a run directory made"


@needs_tclsh
@pytest.mark.parametrize("flow_class, script", PROJECT_MODE)
@pytest.mark.parametrize("mode", ["default", "out_of_context"])
def test_the_mode_of_the_design_goes_through_without_out_of_context(
    flow_class, script, mode, tmp_path, monkeypatch
) -> None:
    run = _run(
        flow_class,
        tmp_path / "run",
        _design(),
        {**SETTINGS, **more_options([f"-mode {mode}"])},
        monkeypatch,
    )
    assert project_mode_options(_script(run, script)) == f"-mode {mode}"


@pytest.mark.parametrize("flow_class", [VivadoSynth, VivadoProject])
def test_the_check_is_made_by_the_flow_class_alone(flow_class, tmp_path) -> None:
    """Planning calls it without a flow, a tool or a run directory."""
    conflicting = flow_class.Settings(
        **{**SETTINGS, "out_of_context": True, **more_options(["-mode default"])}
    )
    with pytest.raises(FlowSettingsException) as error:
        flow_class.check_settings_supported(conflicting)
    assert "out_of_context" in str(error.value) and MORE_OPTIONS in str(error.value)
    for fine in (
        flow_class.Settings(**SETTINGS),
        flow_class.Settings(**{**SETTINGS, "out_of_context": True}),
        flow_class.Settings(**{**SETTINGS, **more_options(["-mode default"])}),
        flow_class.Settings(
            **{**SETTINGS, "out_of_context": True, **more_options(["-mode out_of_context"])}
        ),
    ):
        flow_class.check_settings_supported(fine)


# `vivado_synth.tcl` sets `set_synth_properties` after the steps, so a property for the options of
# `synth_design` replaces what the steps gave, the mode included. (`vivado_project.tcl` does not
# render `set_synth_properties`: only the steps count for it.)
PROPERTY = "STEPS.SYNTH_DESIGN.ARGS.MORE OPTIONS"
PROPERTY_KEY = f"set_synth_properties[{PROPERTY}]"


def with_property(value, name: str = PROPERTY, **settings) -> dict:
    return {**SETTINGS, "set_synth_properties": {name: value}, **settings}


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("-flatten_hierarchy none", id="no-mode"),
        pytest.param("", id="empty"),
        pytest.param("-mode default", id="another-mode"),
    ],
)
@pytest.mark.parametrize("name", [PROPERTY, PROPERTY.lower()], ids=["upper", "lower"])
def test_a_property_that_replaces_the_options_must_carry_the_mode(value, name) -> None:
    settings = VivadoSynth.Settings(**with_property(value, name, out_of_context=True))
    with pytest.raises(FlowSettingsException) as error:
        VivadoSynth.check_settings_supported(settings)
    message = str(error.value)
    assert "out_of_context" in message and f"set_synth_properties[{name}]" in message
    if value != "-mode default":
        assert "after the steps" in message and MORE_OPTIONS in message


@pytest.mark.parametrize(
    "value",
    ["-mode out_of_context", "-flatten_hierarchy none -mode out_of_context", "-mod OUT_OF_CONTEXT"],
)
def test_a_property_that_carries_the_mode_is_fine(value) -> None:
    VivadoSynth.check_settings_supported(
        VivadoSynth.Settings(**with_property(value, out_of_context=True))
    )


def test_a_property_is_not_judged_without_out_of_context() -> None:
    for value in ("-flatten_hierarchy none", "", "-mode default"):
        VivadoSynth.check_settings_supported(VivadoSynth.Settings(**with_property(value)))


def test_a_property_for_something_else_is_not_judged() -> None:
    VivadoSynth.check_settings_supported(
        VivadoSynth.Settings(
            **with_property("-retiming", "STEPS.SYNTH_DESIGN.ARGS.MAX_BRAM", out_of_context=True)
        )
    )


@needs_tclsh
def test_a_property_that_carries_the_mode_holds_it_once_in_the_script(
    tmp_path, monkeypatch
) -> None:
    """The property is set last, so its value is the run's; the mode is in it once. (The
    launcher expands the dots of a property name into a hierarchy, so such a setting reaches a
    flow built directly only.)"""
    use_fake_tools(monkeypatch)
    flow = _built_directly(
        VivadoSynth,
        tmp_path,
        out_of_context=True,
        set_synth_properties={PROPERTY: "-flatten_hierarchy none -mode out_of_context"},
    )
    with WorkingDirectory(tmp_path):
        flow.init()
        flow.run()
    value = project_mode_options((tmp_path / "vivado_synth.tcl").read_text())
    assert value == "-flatten_hierarchy none -mode out_of_context"


# A flow built directly is not planned, so its own render-time check is the only one.
def _built_directly(flow_class, tmp_path, **settings):
    return flow_class(flow_class.Settings(**{**SETTINGS, **settings}), _design(), tmp_path)


@pytest.mark.parametrize("flow_class", [VivadoSynth, VivadoProject])
def test_a_flow_built_directly_refuses_another_mode_in_the_steps(flow_class, tmp_path) -> None:
    flow = _built_directly(
        flow_class, tmp_path, out_of_context=True, **more_options(["-mode default"])
    )
    with pytest.raises(FlowSettingsException) as error:
        flow.run()
    assert "out_of_context" in str(error.value) and MORE_OPTIONS in str(error.value)


@pytest.mark.parametrize("value", ["-flatten_hierarchy none", "-mode default"])
def test_vivado_synth_built_directly_refuses_a_property_without_the_mode(value, tmp_path) -> None:
    flow = _built_directly(
        VivadoSynth, tmp_path, out_of_context=True, set_synth_properties={PROPERTY: value}
    )
    with pytest.raises(FlowSettingsException) as error:
        flow.run()
    assert PROPERTY_KEY in str(error.value)


def test_alt_synth_built_directly_refuses_another_mode(tmp_path) -> None:
    flow = _built_directly(
        VivadoAltSynth, tmp_path, out_of_context=True, **synth_step({"mode": "default"})
    )
    with pytest.raises(FlowSettingsException) as error:
        flow.run()
    assert "out_of_context" in str(error.value) and SYNTH_STEP in str(error.value)


@pytest.mark.parametrize("value", ["-flatten_hierarchy none", "-mode default", ""])
def test_vivado_project_does_not_judge_what_its_script_does_not_apply(value, tmp_path) -> None:
    """Its template renders the steps and not `set_synth_properties`, so the property has no
    effect there, and refusing it would refuse nothing."""
    settings = {"out_of_context": True, "set_synth_properties": {PROPERTY: value}}
    VivadoProject.check_settings_supported(VivadoProject.Settings(**{**SETTINGS, **settings}))
    # built directly, its render-time check passes it too (it fails later, at the missing tool)
    flow = _built_directly(VivadoProject, tmp_path, **settings)
    synth_steps, _ = run_steps(flow.settings, out_of_context=True)
    assert synth_steps["SYNTH_DESIGN"]["ARGS"]["MORE"]["OPTIONS"] == ["-mode out_of_context"]


# ----------------------------------------------------------------------------- vivado_alt_synth


@needs_tclsh
@pytest.mark.parametrize(
    "step",
    [
        pytest.param({"mode": "out_of_context"}, id="mapping"),
        pytest.param({"retiming": True, "mode": "out_of_context"}, id="among-others"),
        pytest.param(["mode out_of_context"], id="list"),
        pytest.param({"mod": "out_of_context"}, id="abbreviated"),
        pytest.param({"mode": "OUT_OF_CONTEXT"}, id="upper-case"),
    ],
)
def test_alt_synth_keeps_the_out_of_context_mode_the_design_gives_once(
    step, tmp_path, monkeypatch
) -> None:
    run = _run(
        VivadoAltSynth,
        tmp_path / "run",
        _design(),
        {**SETTINGS, "out_of_context": True, **synth_step(step)},
        monkeypatch,
    )
    line = synth_design_line(_script(run, "vivado_alt_synth.tcl"))
    assert line.lower().count("out_of_context") == 1 and not line.endswith("-mode"), line


@pytest.mark.parametrize(
    "step, shown",
    [
        pytest.param({"mode": "default"}, "-mode default", id="mapping"),
        pytest.param({"retiming": True, "mode": "default"}, "-mode default", id="among-others"),
        pytest.param(["mode default"], "-mode default", id="list"),
        pytest.param({"mod": "default"}, "-mod default", id="abbreviated"),
        pytest.param({"mode": "Default"}, "-mode Default", id="capitalized"),
    ],
)
def test_alt_synth_refuses_another_mode_with_out_of_context_before_anything_runs(
    step, shown, tmp_path, monkeypatch
) -> None:
    use_fake_tools(monkeypatch)
    root = tmp_path / "run"
    with pytest.raises(FlowSettingsException) as error:
        DefaultRunner(root, display_results=False).run_flow(
            VivadoAltSynth,
            _design(),
            {**SETTINGS, "out_of_context": True, **synth_step(step)},
        )
    message = str(error.value)
    assert "out_of_context" in message and SYNTH_STEP in message and shown in message
    assert not (root / "design0").exists(), "the flow had a run directory made"


@needs_tclsh
@pytest.mark.parametrize("mode", ["default", "out_of_context"])
def test_alt_synth_passes_the_mode_of_the_design_without_out_of_context(
    mode, tmp_path, monkeypatch
) -> None:
    run = _run(
        VivadoAltSynth,
        tmp_path / "run",
        _design(),
        {**SETTINGS, **synth_step({"mode": mode})},
        monkeypatch,
    )
    line = synth_design_line(_script(run, "vivado_alt_synth.tcl"))
    assert line.count("-mode") == 1 and f"-mode {mode}" in line, line


def test_alt_synth_check_is_made_by_the_flow_class_alone() -> None:
    with pytest.raises(FlowSettingsException) as error:
        VivadoAltSynth.check_settings_supported(
            VivadoAltSynth.Settings(
                **{**SETTINGS, "out_of_context": True, **synth_step({"mode": "default"})}
            )
        )
    assert "out_of_context" in str(error.value) and SYNTH_STEP in str(error.value)
    for fine in (
        {},
        {"out_of_context": True},
        synth_step({"mode": "default"}),
        {"out_of_context": True, **synth_step({"mode": "out_of_context"})},
    ):
        VivadoAltSynth.check_settings_supported(VivadoAltSynth.Settings(**{**SETTINGS, **fine}))
