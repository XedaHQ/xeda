"""Simulation times, delays and limits take a unit, and each tool gets the number it expects.

A time setting is a quantity: text with a unit, never a bare number. Each reader converts the
quantity to the unit of its tool. The expected command lines and scripts here are the ones that
the settings gave before they had units, written with a unit now.
"""

import json
from pathlib import Path

import pytest

from xeda.flow import FlowSettingsError, SimFlow
from xeda.flow_runner import DefaultRunner, get_flow_class
from xeda.flow_runner.error_codes import settings_codes
from xeda.flows import GhdlSim, Nvc, Vcs
from xeda.tool import Tool
from xeda.units import Time

from . import test_ghdl, test_modelsim, test_vcs_sim_evidence, test_vivado_sim_evidence
from .settings_samples import flow_classes, minimal_settings
from .test_physical_quantities import NOT_MIGRATED_YET
from .tool_utils import require_ghdl, require_nvc

SIMULATORS = [cls for cls, _ in flow_classes() if issubclass(cls, SimFlow)]
CLOCK = "clk <= not clk after 5 ns;\n"


# ------------------------------------------------------------------------------ the type


def test_a_timeout_takes_a_quantity_and_refuses_a_bare_number():
    ghdl = get_flow_class("ghdl_sim")
    assert ghdl.Settings(timeout="2 min").timeout.to("s") == 120
    with pytest.raises(FlowSettingsError) as refused:
        ghdl.Settings.from_input({"timeout": 120})
    assert settings_codes(refused.value) == ["invalid_quantity"]


@pytest.mark.parametrize("cls", SIMULATORS, ids=lambda cls: cls.name)
@pytest.mark.parametrize("name", ["timeout", "stop_time"])
def test_every_simulator_takes_its_times_with_a_unit(cls, name):
    settings = minimal_settings(cls)
    assert cls.Settings(**settings, **{name: "2 min"}).model_dump()[name] == Time("2 min")
    for bare in (120, 2.5, "120", True):
        with pytest.raises(FlowSettingsError) as refused:
            cls.Settings.from_input({**settings, name: bare})
        assert settings_codes(refused.value) == ["invalid_quantity"], bare


def test_a_timeout_must_be_positive_and_a_stop_time_must_not_be_negative():
    ghdl = get_flow_class("ghdl_sim")
    for refused in ("0 s", "-1 s"):
        with pytest.raises(FlowSettingsError):
            ghdl.Settings.from_input({"timeout": refused})
    assert ghdl.Settings(stop_time="0 ns").stop_time == Time("0 ns")
    with pytest.raises(FlowSettingsError):
        ghdl.Settings.from_input({"stop_time": "-1 ns"})


def test_a_design_file_refuses_a_bare_number_too(tmp_path):
    (tmp_path / "t.vhd").write_text("entity t is end; architecture a of t is begin end;\n")
    design = tmp_path / "design.yaml"
    for text, codes in (("120", ["invalid_quantity"]), ("2 min", None)):
        design.write_text(
            f"name: t\nrtl:\n  top: t\n  sources: [t.vhd]\nflows:\n  ghdl_sim:\n    timeout: {text}\n"
        )
        runner = DefaultRunner(tmp_path / "run")
        if codes is None:
            runner.plan("ghdl_sim", design)
        else:
            with pytest.raises(FlowSettingsError) as refused:
                runner.plan("ghdl_sim", design)
            assert settings_codes(refused.value) == codes


def test_the_timeout_reaches_a_tool_in_seconds():
    settings = get_flow_class("ghdl_sim").Settings(timeout="2 min")
    assert settings.timeout_in_seconds() == 120.0
    assert get_flow_class("ghdl_sim").Settings().timeout_in_seconds() is None


def test_not_migrated_yet_holds_only_the_design_space_search():
    assert {owner for owner, _ in NOT_MIGRATED_YET} <= {"Dse.Settings", "FmaxOptimizer.Settings"}


# ------------------------------------------------------------------------------ the tools


def record_tool_runs(monkeypatch):
    """Record the name, the arguments and the time limit of every tool run, and run the tool."""
    runs = []
    original = Tool.execute

    def execute(tool, executable, *args, **kwargs):
        runs.append((Path(executable).name, [str(a) for a in args], kwargs.get("timeout")))
        return original(tool, executable, *args, **kwargs)

    monkeypatch.setattr(Tool, "execute", execute)
    return runs


@pytest.mark.parametrize(
    "stop_time,flag",
    [
        ("100 ns", "--stop-time=100000000fs"),
        ("100 us", "--stop-time=100000000000fs"),
        ("1.5 us", "--stop-time=1500000000fs"),
    ],
)
def test_ghdl_gets_the_stop_time_in_femtoseconds_and_the_timeout_in_seconds(
    tmp_path, monkeypatch, stop_time, flag
):
    require_ghdl()
    runs = record_tool_runs(monkeypatch)
    design = test_ghdl._evidence_design(tmp_path, "", clock=CLOCK)
    settings = {"stop_time": stop_time, "timeout": "1.5 min"}
    DefaultRunner(tmp_path / "run").run_flow(GhdlSim, design, settings)
    (run,) = [run for run in runs if run[1][:1] == ["run"]]
    assert run == (
        "ghdl",
        [
            "run",
            "--std=08",
            "tb",
            "--assert-level=error",
            "--ieee-asserts=disable-at-0",
            flag,
            "--unbuffered",
        ],
        90.0,
    )


@pytest.mark.parametrize(
    "stop_time,flag",
    [
        ("100 ns", "--stop-time=100000000fs"),
        ("100 us", "--stop-time=100000000000fs"),
        ("1.5 us", "--stop-time=1500000000fs"),
    ],
)
def test_nvc_gets_the_stop_time_in_femtoseconds_and_the_timeout_in_seconds(
    tmp_path, monkeypatch, stop_time, flag
):
    require_nvc()
    runs = record_tool_runs(monkeypatch)
    design = test_ghdl._evidence_design(tmp_path, "", clock=CLOCK)
    settings = {"stop_time": stop_time, "timeout": "90 s"}
    DefaultRunner(tmp_path / "run").run_flow(Nvc, design, settings)
    (run,) = [run for run in runs if flag in run[1]]
    assert run[2] == 90.0


@pytest.mark.parametrize(
    "stop_time,run",
    [("100 us", "100000000.0ps"), ("1.5 us", "1500000.0ps"), ("100 ns", "100000.0ps")],
)
def test_modelsim_gets_the_stop_time_in_picoseconds(tmp_path, monkeypatch, stop_time, run):
    flow, calls = test_modelsim._run(tmp_path, monkeypatch, {"stop_time": stop_time})
    assert ["run", run] in calls


@pytest.mark.parametrize(
    "stop_time,prerun,stop_fs,prerun_fs",
    [
        ("100 ns", "10 ns", "100000000", "10000000"),
        ("100 us", "0.01 us", "100000000000", "10000000"),
        ("1.5 us", "10000 ps", "1500000000", "10000000"),
    ],
)
def test_xsim_gets_the_stop_and_pre_run_times_in_femtoseconds_and_the_timeout_in_seconds(
    tmp_path, monkeypatch, stop_time, prerun, stop_fs, prerun_fs
):
    runs = record_tool_runs(monkeypatch)
    settings = {"stop_time": stop_time, "prerun_time": prerun, "timeout": "1.5 min"}
    flow = test_vivado_sim_evidence.launch(tmp_path, monkeypatch, "clock", settings)
    script = (flow.run_path / "vivado_sim.tcl").read_text().splitlines()
    assert f"set xeda_stop_fs {stop_fs}" in script
    assert f"set xeda_prerun_fs {prerun_fs}" in script
    assert 90.0 in [run[2] for run in runs if run[0] == "vivado"]


def vcs_commands(flow) -> dict[str, list[str]]:
    """The arguments of each VCS program that the fake recorded, by program."""
    lines = (flow.run_path / "fake_vcs.invocations").read_text().splitlines()
    return {name: args for name, args in map(json.loads, lines)}


def test_vcs_gets_its_times_in_the_spelling_of_vcs(tmp_path, monkeypatch):
    settings = {"stop_time": "100 us", "timeout": "90 s"}
    flow = test_vcs_sim_evidence.launch(tmp_path, monkeypatch, "limit10", settings=settings)
    commands = vcs_commands(flow)
    assert "-timescale=1ns/1ps" in commands["vlogan"]
    vcs = commands["vcs"]
    assert vcs[vcs.index("-vc_lic_wait") + 1] == "100"
    assert "-sim_res=1ps" in vcs
    assert (
        'set xeda_limit [stop -absolute "100000000000fs"]'
        in (flow.run_path / "vcs_runtime.tcl").read_text()
    )


def test_vcs_gets_another_time_scale_and_license_wait_as_it_spells_them(tmp_path, monkeypatch):
    settings = {"time_unit": "100 ps", "time_resolution": "10 fs", "lic_wait": "2 h"}
    flow = test_vcs_sim_evidence.launch(tmp_path, monkeypatch, "finish5", settings=settings)
    commands = vcs_commands(flow)
    assert "-timescale=100ps/10fs" in commands["vlogan"]
    vcs = commands["vcs"]
    assert "-sim_res=10fs" in vcs
    assert vcs[vcs.index("-vc_lic_wait") + 1] == "120"


@pytest.mark.parametrize(
    "value,text",
    [
        ("1 s", "1s"),
        ("10 ms", "10ms"),
        ("100 us", "100us"),
        ("1000 ps", "1ns"),
        ("0.1 ns", "100ps"),
    ],
)
def test_the_time_scale_of_vcs_is_one_ten_or_a_hundred_of_a_unit(value, text):
    from xeda.flows.vcs import timescale_text

    assert timescale_text(Time(value)) == text


@pytest.mark.parametrize(
    "settings,message",
    [
        ({"time_unit": "2 ns"}, "it takes 1, 10 or 100"),
        ({"time_resolution": "1 ns 5"}, "cannot read"),
        ({"lic_wait": "90 s"}, "whole number of minutes"),
        ({"time_unit": "1"}, "needs a unit"),
        ({"lic_wait": 100}, "needs a unit"),
    ],
)
def test_vcs_refuses_a_time_it_cannot_take(settings, message):
    with pytest.raises(Exception) as refused:
        Vcs.check_settings_supported(Vcs.Settings(**settings))
    assert message in str(refused.value)


@pytest.mark.parametrize(
    "settings,flag",
    [
        ({"freq": "6 MHz"}, "6000000"),
        ({"freq": "6000 kHz"}, "6000000"),
        ({"freq": "750 Hz"}, "750"),
    ],
)
def test_the_loader_gets_its_frequency_in_whole_hertz(settings, flag):
    from xeda.flows import Openfpgaloader

    ss = Openfpgaloader.Settings(fpga={"part": "LFE5U-25F-6BG381C"}, **settings)
    Openfpgaloader.check_settings_supported(ss)
    assert ss.freq.to("Hz") == int(flag)
