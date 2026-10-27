"""The settings that every flow has: one verbosity setting and one Docker switch.

`verbosity` replaces `verbose`, `quiet` and `debug`. `docker` switches Docker on, and
`docker_image` names the image. The three settings of the terminal output went to the workspace.
"""

import json
import pytest
from pydantic import ValidationError

from xeda import Design
from xeda.flow import Flow, FlowSettingsError, registered_flows
from xeda.flow_runner import get_flow_class
from xeda.flows import Bsc, Nextpnr, VivadoSynth, Yosys, YosysFpga

from .test_nextpnr import _nextpnr_args
from .test_vcs_sim_evidence import launch as launch_vcs

HIDDEN = {"reports_dir", "checkpoints_dir", "outputs_dir"}
FLOWS = sorted(
    {cls for _, cls in registered_flows.values() if not cls.__name__.startswith("_")},
    key=lambda cls: cls.name,
)


def test_the_settings_of_every_flow_are_the_contract_set():
    assert set(Flow.Settings.model_fields) - HIDDEN == {
        "verbosity",
        "nthreads",
        "lib_paths",
        "docker",
        "docker_image",
    }


def test_the_defaults_are_normal_verbosity_and_no_docker():
    settings = Flow.Settings()
    assert (settings.verbosity, settings.docker, settings.docker_image) == ("normal", False, None)


@pytest.mark.parametrize("value", ["loud", "Debug", 1, True, None])
def test_verbosity_takes_one_of_its_four_words(value):
    with pytest.raises(FlowSettingsError):
        Nextpnr.Settings.from_input({"verbosity": value})


def test_docker_is_a_switch_and_the_image_has_its_own_setting():
    settings = Nextpnr.Settings.from_input({"docker": True, "docker_image": "example/nextpnr:1"})
    assert settings.docker is True and settings.docker_image == "example/nextpnr:1"
    hint = r"write `docker: true`, and give the image as `docker_image: example/nextpnr:1`"
    with pytest.raises(FlowSettingsError, match=hint):
        Nextpnr.Settings.from_input({"docker": "example/nextpnr:1"})
    with pytest.raises(ValidationError, match=hint):
        Nextpnr.Settings().docker = "example/nextpnr:1"
    with pytest.raises(FlowSettingsError, match="`yes` is text, not a boolean: write `true`"):
        Nextpnr.Settings.from_input({"docker": "yes"})  # a boolean word gets the boolean message


@pytest.mark.parametrize(
    "flow, name, successor",
    [
        ("nextpnr", "verbose", "`verbosity`"),
        ("nextpnr", "quiet", "`verbosity`"),
        ("nextpnr", "debug", "`verbosity`"),
        ("nextpnr", "dockerized", "`docker`"),
        ("vcs", "quiet", "`verbosity`"),
    ],
)
def test_a_removed_base_setting_names_its_successor(flow, name, successor):
    with pytest.raises(FlowSettingsError) as error:
        get_flow_class(flow).Settings.from_input({name: True})
    assert f"`{name}` was removed: use {successor}" in str(error.value)


@pytest.mark.parametrize(
    "flow, name",
    [
        ("ghdl_sim", "redirect_stdout"),
        ("ghdl_sim", "print_commands"),
        ("vivado_synth", "console_colors"),
    ],
)
def test_a_removed_output_setting_says_why_it_has_no_successor(flow, name):
    with pytest.raises(FlowSettingsError) as error:
        get_flow_class(flow).Settings.from_input({name: True})
    assert (
        f"`{name}` was removed: where the output of the tools appears is not a setting of a flow;"
        " delete it" in str(error.value)
    )


@pytest.mark.parametrize("flow_class", FLOWS, ids=lambda cls: cls.name)
def test_no_flow_redeclares_a_verbosity_switch(flow_class):
    assert not {"quiet", "verbose", "debug"} & set(flow_class.Settings.model_fields)


# ---------------------------------------------------------------- what each verbosity renders


@pytest.mark.parametrize(
    "verbosity, flags, absent",
    [
        ("quiet", ["--quiet"], ["--verbose", "--debug"]),
        ("normal", [], ["--quiet", "--verbose", "--debug"]),
        ("verbose", ["--verbose"], ["--quiet", "--debug"]),
        ("debug", ["--debug"], ["--quiet", "--verbose"]),
    ],
)
def test_nextpnr_renders_verbosity(tmp_path, monkeypatch, verbosity, flags, absent):
    args = _nextpnr_args(
        tmp_path, monkeypatch, fpga={"family": "ecp5", "capacity": "25k"}, verbosity=verbosity
    )
    assert all(flag in args for flag in flags)
    assert not any(flag in args for flag in absent)


def _yosys_flags(flow_class, tmp_path, **settings):
    """The default arguments of the yosys program of a flow."""
    design = Design(name="top", root=tmp_path, rtl={"sources": [], "top": "top"})
    flow = flow_class(flow_class.Settings.from_input(settings), design, tmp_path / "run")
    return flow.yosys.default_args


@pytest.mark.parametrize("flow_class", [Yosys, YosysFpga])
@pytest.mark.parametrize(
    "verbosity, present, absent",
    [
        ("quiet", ["-T", "-Q", "-q"], ["-g"]),
        ("normal", ["-T", "-Q"], ["-q", "-g"]),
        ("verbose", [], ["-T", "-Q", "-q", "-g"]),
        ("debug", ["-g"], ["-T", "-Q", "-q"]),
    ],
)
def test_yosys_renders_verbosity(flow_class, tmp_path, verbosity, present, absent):
    flags = _yosys_flags(flow_class, tmp_path, verbosity=verbosity)
    assert all(flag in flags for flag in present)
    assert not any(flag in flags for flag in absent)


@pytest.mark.parametrize(
    "verbosity, notrace, verbose",
    [
        ("quiet", True, False),
        ("normal", True, False),
        ("verbose", True, True),
        ("debug", False, False),
    ],
)
def test_vivado_renders_verbosity(tmp_path, verbosity, notrace, verbose):
    design = Design(name="top", root=tmp_path, rtl={"sources": [], "top": "top"})
    settings = VivadoSynth.Settings.from_input({"verbosity": verbosity})
    flow = VivadoSynth(settings, design, tmp_path / "run")
    flow.init()
    assert ("-notrace" in flow.vivado.default_args) is notrace
    assert ("-verbose" in flow.vivado.default_args) is verbose


@pytest.mark.parametrize(
    "verbosity, flag",
    [("quiet", "-quiet"), ("normal", None), ("verbose", "-verbose"), ("debug", None)],
)
def test_bsc_renders_verbosity(tmp_path, verbosity, flag):
    design = Design(name="top", root=tmp_path, rtl={"sources": [], "top": "top"})
    flow = Bsc(Bsc.Settings.from_input({"verbosity": verbosity}), design, tmp_path / "run")
    assert flow._verbosity_flags() == ([flag] if flag else [])


def _vcs_arguments(tmp_path, monkeypatch, verbosity):
    flow = launch_vcs(tmp_path, monkeypatch, "finish5", settings={"verbosity": verbosity})
    calls = [
        json.loads(line)
        for line in (flow.run_path / "fake_vcs.invocations").read_text().splitlines()
    ]
    return {name: args for name, args in calls}


@pytest.mark.parametrize("verbosity", ["quiet", "normal", "verbose", "debug"])
def test_vcs_renders_verbosity(tmp_path, monkeypatch, verbosity):
    arguments = _vcs_arguments(tmp_path, monkeypatch, verbosity)
    assert ("-q" in arguments["vcs"]) is (verbosity == "quiet")
    assert ("-notice" in arguments["vcs"]) is (verbosity == "verbose")
