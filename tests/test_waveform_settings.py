"""The waveform settings of every simulator.

One rule reads them (`dataclass.waveform_value`, applied by `Flow.Settings`): the text `true` and
`false` are the booleans, so `-s vcd=true` and `vcd: true` are one setting. A simulator either
writes the waveform through its own mechanism or refuses the setting when planned; none ignores
it. The sweeps below classify every waveform setting of every simulator, so a new setting or
simulator fails here until it is classified.
"""

from pathlib import Path, PurePath

import pytest
from pydantic import ValidationError

from xeda import Design
from xeda.dataclass import (
    WaveformText,
    waveform_fields,
    waveform_format,
    waveform_format_of,
    waveform_value,
)
from xeda.flow import FlowSettingsError, FlowSettingsException, SimFlow
from xeda.flow.flow import identity_settings
from xeda.flow_runner import DefaultRunner, get_flow_class
from xeda.flows import GhdlSim, Nvc

from .settings_samples import flow_classes, minimal_settings
from .sim_evidence_cases import CASES, SimCase, launch_case
from .test_ghdl import _evidence_design
from .tool_utils import fake_calls, require_ghdl, require_nvc_evidence

SIM_FLOWS = [(cls, name) for cls, name in flow_classes() if issubclass(cls, SimFlow)]
FIELDS = [
    pytest.param(cls, name, field, extension, id=f"{name}.{field}")
    for cls, name in SIM_FLOWS
    for field, extension in waveform_fields(cls.Settings).items()
]


def true_name(cls, field: str) -> str | None:
    """The name `true` stands for, None where the setting has none."""
    found = waveform_format(cls.Settings, field)
    assert found is not None
    return found[1]


def build(cls, **settings):
    return cls.Settings.from_input({**minimal_settings(cls), **settings})


# ---------------------------------------------------------------------------------------------
# one rule for the text and the boolean
# ---------------------------------------------------------------------------------------------


def test_the_marker_is_easy_to_query_by_field():
    settings = get_flow_class("nvc").Settings
    assert waveform_format_of(settings, "vcd") == "vcd"
    assert waveform_format_of(settings, "wave") == "fst"
    assert waveform_format_of(settings, "timeout") is None
    assert waveform_format_of(settings, "no_such_setting") is None


def test_the_sweep_sees_the_simulators_and_their_waveform_settings():
    assert {name for _, name in SIM_FLOWS} >= {"ghdl_sim", "nvc", "verilator", "vcs", "yosys_sim"}
    assert {param.id for param in FIELDS} >= {
        "ghdl_sim.vcd",
        "ghdl_sim.wave",
        "ghdl_sim.fst",
        "nvc.wave",
        "verilator.saif",
        "vcs.fsdb",
        "vivado_sim.saif",
    }


@pytest.mark.parametrize("cls,name,field,extension", FIELDS)
def test_the_text_true_or_false_is_the_boolean(cls, name, field, extension):
    """`-s <setting>=true` and `<setting>: true` are one setting, for every waveform setting of
    every simulator: the command line can only write text."""
    false_text, false_bool = build(cls, **{field: "false"}), build(cls, **{field: False})
    assert getattr(false_text, field) is getattr(false_bool, field) is None
    assert false_text.model_dump() == false_bool.model_dump()
    assert getattr(build(cls, **{field: ""}), field) is None
    for text in ("true", "True"):
        if true_name(cls, field) is None:
            with pytest.raises(FlowSettingsError, match="has no file name"):
                build(cls, **{field: text})
            continue
        true_text, true_bool = build(cls, **{field: text}), build(cls, **{field: True})
        assert getattr(true_text, field) == getattr(true_bool, field) is not None
        assert true_text.model_dump() == true_bool.model_dump()
        settings = build(cls)  # an assignment is read by the same rule
        setattr(settings, field, text)
        assert getattr(settings, field) == getattr(true_bool, field)


@pytest.mark.parametrize("cls,name,field,extension", FIELDS)
def test_the_yaml_boolean_words_are_refused_like_a_boolean_settings(cls, name, field, extension):
    """A setting is on, off or a name, so `yes` is neither: it names the boolean to write, as for
    a boolean setting, and the file name that would take its place."""
    for word in ("yes", "No", "on", "OFF", "y", "n"):
        with pytest.raises(FlowSettingsError, match=rf"`{word}` is text, not a boolean: write"):
            build(cls, **{field: word})
        with pytest.raises(ValidationError, match="not a boolean"):
            setattr(build(cls), field, word)
    with pytest.raises(FlowSettingsError, match=r"write `true` \(or give a file name\)"):
        build(cls, **{field: "yes"})
    # as part of a name, such a word is a name
    assert "yes" in str(getattr(build(cls, **{field: "yes.out"}), field))


@pytest.mark.parametrize("cls,name,field,extension", FIELDS)
def test_true_stands_for_the_name_a_location_is_written_under(cls, name, field, extension):
    """A run is told by what it writes, so `true` and a location are one run when they write one
    file: the name `true` stands for is the conventional name of a location."""
    expected = true_name(cls, field)
    conventional = build(cls).conventional_output(field, "design")
    assert conventional is not None
    if expected is None:
        assert "design" in str(conventional)  # the name holds the design's: `true` cannot say it
        return
    assert PurePath(expected) == conventional
    on = identity_settings(build(cls, **{field: "true"}), "design")
    located = identity_settings(build(cls, **{field: "/elsewhere/w.out"}), "design")
    assert on.model_dump(mode="json") == located.model_dump(mode="json")
    named = getattr(build(cls, **{field: "wave"}), field)
    assert Path(named) == Path(f"wave.{extension}")
    kept = getattr(build(cls, **{field: "wave.out"}), field)
    assert Path(kept) == Path("wave.out")


@pytest.mark.parametrize(
    "value,extension,name,expected",
    [
        (True, "vcd", "dump.vcd", "dump.vcd"),
        ("true", "vcd", "dump.vcd", "dump.vcd"),
        ("TRUE", "fst", "dump.fst", "dump.fst"),
        (False, "vcd", "dump.vcd", None),
        ("false", "vcd", "dump.vcd", None),
        ("", "vcd", "dump.vcd", None),
        (None, "vcd", "dump.vcd", None),
        ("w", "vcd", "dump.vcd", "w.vcd"),
        ("w.fst", "vcd", "dump.vcd", "w.fst"),
        ("yes.out", "vcd", "dump.vcd", "yes.out"),
        (Path("d/w"), "vcd", "dump.vcd", "d/w.vcd"),
        (3, "vcd", "dump.vcd", 3),
        ([], "vcd", "dump.vcd", []),
    ],
)
def test_the_rule(value, extension, name, expected):
    assert waveform_value(value, extension, name) == expected


@pytest.mark.parametrize("value", ["yes", "YES", "no", "on", "off", "y", "n", "t", "f", " on "])
def test_the_rule_refuses_the_words_yaml_1_1_reads_as_booleans(value):
    with pytest.raises(WaveformText, match="is text, not a boolean"):
        waveform_value(value, "vcd", "dump.vcd")


def test_true_is_refused_where_the_setting_has_no_name_for_it():
    with pytest.raises(WaveformText, match="has no file name"):
        waveform_value("true", "saif", None)
    assert waveform_value("false", "saif", None) is None


def test_the_command_line_text_gives_the_run_a_boolean_waveform_would(tmp_path):
    """A run planned from `-s vcd=true` is the run planned from `vcd: true`: the same settings and
    the same identity."""
    design = _evidence_design(tmp_path, "std.env.finish;")
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    text = runner.plan("ghdl_sim", design, flow_settings=["vcd=true"]).node("ghdl_sim")
    boolean = runner.plan("ghdl_sim", design, flow_settings={"vcd": True}).node("ghdl_sim")
    assert text.settings.vcd == "dump.vcd"
    assert text.settings.model_dump() == boolean.settings.model_dump()
    assert text.flowrun_hash == boolean.flowrun_hash


# ---------------------------------------------------------------------------------------------
# no waveform setting is ignored
# ---------------------------------------------------------------------------------------------

#: What each simulator hands its tool for a waveform setting set to `true`. Every item is an
#: argument of a tool command, or a text in a script xeda wrote for the tool. A setting a
#: simulator cannot honor is not here: it is `unsupported_waveforms`, refused when planned.
HONORED: dict[tuple[str, str | None, str], list[str]] = {
    ("ghdl_sim", None, "vcd"): ["--vcd=dump.vcd"],
    ("ghdl_sim", None, "fst"): ["--fst=dump.fst"],
    ("ghdl_sim", None, "wave"): ["--wave=dump.ghw"],
    ("nvc", None, "vcd"): ["--wave=dump.vcd", "--format=vcd"],
    ("nvc", None, "wave"): ["--wave=dump.fst"],
    ("modelsim", None, "vcd"): ["vcd file dump.vcd", "vcd add -r"],
    ("vcs", None, "fsdb"): ['dump -file "dump.fsdb" -type FSDB'],
    ("vcs", None, "vpd"): ['dump -file "dump.vpd" -type vpd'],
    ("vcs", None, "evcd"): ['dump -file "dump.evcd" -type evcd'],
    ("vivado_sim", None, "vcd"): ["open_vcd dump.vcd"],
    ("vivado_sim", None, "saif"): ["open_saif wave.saif"],
    ("vivado_postsynth_sim", None, "vcd"): ["open_vcd dump.vcd"],
    ("vivado_postsynth_sim", None, "saif"): ["open_saif wave.saif"],
    ("verilator", None, "vcd"): ["--trace-vcd"],
    ("verilator", None, "fst"): ["--trace-fst"],
    ("verilator", None, "saif"): ["--trace-saif"],
    ("bsc_sim", "bluesim", "vcd"): ["-V", "dump.vcd"],
    ("bsc_sim", "verilator", "vcd"): ["+bscvcd"],
}
#: bsc's Verilog drivers take the name as a plusarg
for _backend in ("iverilog", "modelsim", "questa", "vcs", "vcsi", "xsim"):
    HONORED[("bsc_sim", _backend, "vcd")] = ["+bscvcd=dump.vcd"]


def waveform_cases():
    for case in CASES:
        cls = get_flow_class(case.flow)
        for field in waveform_fields(cls.Settings):
            yield pytest.param(case, field, id=f"{case.name}.{field}")


def run_text(flow, calls) -> tuple[set[str], str]:
    """What the tool is handed: the arguments of every tool command, and the text of the scripts
    xeda wrote for it (including the commands a fake Tcl tool recorded)."""
    words = {word for _, args in calls for word in args}
    text = []
    for path in flow.run_path.glob("*"):
        if path.suffix in (".tcl", ".do") and not path.name.startswith("fake_"):
            text.append(path.read_text(encoding="utf-8"))
    try:
        text += [" ".join(call) for call in fake_calls(flow.run_path)]
    except FileNotFoundError:
        pass
    return words, "\n".join(text)


@pytest.mark.parametrize("case,field", list(waveform_cases()))
def test_a_waveform_setting_reaches_the_tool_or_is_refused_when_planned(
    case: SimCase, field, tmp_path, monkeypatch
):
    cls = get_flow_class(case.flow)
    refused = cls.unsupported_waveforms.get(field)
    key = (case.flow, case.backend, field)
    assert (refused is None) == (key in HONORED), (
        f"{case.name}: classify the waveform setting `{field}`, as honored (HONORED, with what "
        "the tool is handed) or refused (`unsupported_waveforms`), not both and not neither"
    )
    if refused is not None:
        with pytest.raises(FlowSettingsException) as raised:
            launch_case(case, tmp_path, monkeypatch, settings_overrides={field: "true"})
        assert f"cannot write a waveform from `{field}`" in str(raised.value)
        return
    calls: list = []
    # `true` where the setting has a name for it; else the name `HONORED` expects
    given = "true" if true_name(cls, field) else "wave.saif"
    flow = launch_case(
        case, tmp_path, monkeypatch, settings_overrides={field: given}, calls_out=calls
    )
    words, text = run_text(flow, calls)
    for needle in HONORED[key]:
        assert needle in words or needle in text, (
            f"{case.name} was given `{field}: {given}` and never handed {needle!r} to its tool: "
            f"{calls}"
        )


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.name)
def test_a_simulator_that_is_given_no_waveform_is_asked_for_none(
    case: SimCase, tmp_path, monkeypatch
):
    """The other half: no waveform is written, nor asked for, unless a setting says so."""
    calls: list = []
    flow = launch_case(case, tmp_path, monkeypatch, calls_out=calls)
    words, text = run_text(flow, calls)
    asked = ("--vcd", "--fst", "--wave", "--trace-", "open_vcd", "open_saif", "vcd file")
    asked += ("vcd add", "dump -file", "+bscvcd")
    found = [item for item in asked if any(word.startswith(item) for word in words) or item in text]
    assert found == []
    assert case.backend != "bluesim" or "-V" not in words


def test_the_options_that_refine_a_waveform_reach_the_tool(tmp_path, monkeypatch):
    calls: list = []
    launch_case(
        SimCase("nvc"),
        tmp_path / "nvc",
        monkeypatch,
        settings_overrides={
            "vcd": "true",
            "wave_include_glob": "*:keep",
            "wave_exclude_glob": "*:drop",
            "wave_arrays": 8,
        },
        calls_out=calls,
    )
    (run,) = [words for _, words in calls if "-r" in words]
    assert {"--wave=dump.vcd", "--format=vcd", "--include=*:keep", "--exclude=*:drop"} <= set(run)
    assert "--dump-arrays=8" in run
    calls = []
    launch_case(
        SimCase("vcs"),
        tmp_path / "vcs",
        monkeypatch,
        settings_overrides={"vpd": "true", "vpd_size_limit": 4},
        calls_out=calls,
    )
    words = {word for _, args in calls for word in args}
    assert "+vpdfilesize+4" in words
    assert not [word for word in words if word.startswith("+fsdb")]


def test_a_refused_waveform_is_refused_when_planned_and_starts_no_tool(tmp_path):
    design = Design(
        name="d",
        design_root=tmp_path,
        rtl={"sources": [_write(tmp_path / "d.v", "module d; endmodule\n")], "top": "d"},
    )
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    with pytest.raises(FlowSettingsException, match=r"vcs cannot write a waveform from `vcd`.*vpd"):
        runner.plan("vcs", design, flow_settings=["vcd=true"])
    with pytest.raises(FlowSettingsException, match=r"yosys_sim cannot write.*`vcd`.*vcd_writer"):
        runner.plan("yosys_sim", design, flow_settings=["vcd=x.vcd"])
    assert not (tmp_path / "run").exists()


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


# ---------------------------------------------------------------------------------------------
# settings that refine a waveform need one
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flow,settings,match",
    [
        ("nvc", {"wave_format": "vcd"}, "wave_format. refines the waveform"),
        ("nvc", {"wave": "false", "wave_format": "vcd"}, "wave_format. refines the waveform"),
        ("nvc", {"wave_arrays": 8}, "wave_arrays. refines the waveform"),
        ("nvc", {"wave_include_glob": "*:x"}, "wave_include_glob. refines the waveform"),
        ("nvc", {"wave_exclude_glob": "*:x"}, "wave_exclude_glob. refines the waveform"),
        ("nvc", {"vcd": "true", "wave": "true"}, "one waveform, and vcd and wave are set"),
        (
            "nvc",
            {"wave": "true", "wave_format": "vcd"},
            "wave_format is vcd, and wave names dump.fst",
        ),
        (
            "nvc",
            {"wave": "w.vcd", "wave_format": "fst"},
            "wave_format is fst, and wave names w.vcd",
        ),
        ("nvc", {"vcd": "true", "wave_format": "fst"}, "vcd. writes a VCD file"),
        ("ghdl_sim", {"vcd": "true", "fst": "true"}, "one waveform, and vcd, fst are set"),
        ("ghdl_sim", {"wave": "true", "fst": "true"}, "one waveform, and fst, wave are set"),
        ("ghdl_sim", {"read_wave_opt": "w.opt"}, "read_wave_opt. filters the waveform"),
        ("vcs", {"vpd": "true", "fsdb": "true"}, "one waveform, and fsdb, vpd are set"),
        ("vcs", {"to_vcd": True}, "to_vcd. converts an FSDB or VPD waveform"),
        ("vcs", {"to_vcd": True, "evcd": "true"}, "to_vcd. converts an FSDB or VPD waveform"),
        ("vcs", {"vpd_size_limit": 4}, "vpd_size_limit. limits the VPD waveform"),
        ("vcs", {"fsdb_size_limit": 7}, "fsdb_size_limit. limits the FSDB waveform"),
        ("vivado_sim", {"vcd_scope": "/tb"}, "vcd_scope. selects what the VCD holds"),
        ("vivado_sim", {"vcd_level": 2}, "vcd_level. selects what the VCD holds"),
        ("verilator", {"trace_structs": True}, "trace_structs. refines the trace"),
        ("verilator", {"trace_max_width": 8}, "trace_max_width. refines the trace"),
    ],
)
def test_a_waveform_setting_that_needs_another_is_refused_without_it(flow, settings, match):
    cls = get_flow_class(flow)
    with pytest.raises(FlowSettingsException, match=match):
        cls.check_settings_supported(build(cls, **settings))


@pytest.mark.parametrize(
    "flow,settings",
    [
        ("nvc", {"wave": "w.vcd", "wave_format": "vcd"}),
        ("nvc", {"wave": "w", "wave_format": "fst"}),
        ("nvc", {"vcd": "true", "wave": "false"}),
        ("nvc", {"vcd": "true", "wave_format": "vcd", "wave_include_glob": "*:x"}),
        ("ghdl_sim", {"wave": "w.vcd"}),
        ("ghdl_sim", {"vcd": "true", "read_wave_opt": "w.opt"}),
        ("vcs", {"vpd": "true", "to_vcd": True, "vpd_size_limit": 4}),
        ("vcs", {"fsdb": "true", "to_vcd": True, "fsdb_size_limit": 7}),
        ("vcs", {}),
        ("nvc", {"wave": "true", "wave_arrays": 8}),
        ("nvc", {}),
        ("vivado_sim", {"vcd": "true", "vcd_scope": "/tb", "vcd_level": 2}),
        ("verilator", {"fst": "true", "trace_structs": True}),
    ],
)
def test_the_same_settings_with_their_waveform_are_accepted(flow, settings):
    cls = get_flow_class(flow)
    cls.check_settings_supported(build(cls, **settings))


# ---------------------------------------------------------------------------------------------
# real tools
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "setting,written",
    [("vcd", "dump.vcd"), ("fst", "dump.fst"), ("wave", "dump.ghw")],
)
def test_ghdl_writes_the_waveform_the_command_line_asks_for(tmp_path, setting, written):
    require_ghdl()
    design = _evidence_design(tmp_path, "wait for 5 ns; std.env.finish;")
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    flow = runner.run_flow(GhdlSim, design, flow_settings=[f"{setting}=true"])
    assert flow is not None and flow.succeeded
    assert (flow.run_path / written).stat().st_size > 0
    assert not list(flow.run_path.glob("true*"))


@pytest.mark.parametrize(
    "setting,value,written,fmt",
    [
        ("vcd", "true", "dump.vcd", "vcd"),
        ("vcd", "waves.vcd", "waves.vcd", "vcd"),
        ("wave", "w.fst", "w.fst", "fst"),
        ("wave", "w.vcd", "w.vcd", "vcd"),
    ],
)
def test_nvc_writes_the_waveform_through_its_own_option(tmp_path, setting, value, written, fmt):
    """The shared `vcd` is `--wave=<file> --format=vcd`, as `wave: <file>.vcd` is."""
    require_nvc_evidence()
    design = _evidence_design(tmp_path, "wait for 5 ns; std.env.finish;")
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    flow = runner.run_flow(Nvc, design, flow_settings=[f"{setting}={value}"])
    assert flow is not None and flow.succeeded
    wave = flow.run_path / written
    assert wave.stat().st_size > 0
    head = wave.read_bytes()[:16]
    assert (b"$date" in head or b"$version" in head or b"$timescale" in head) == (fmt == "vcd")


def test_nvc_writes_dump_fst_for_wave_true(tmp_path):
    require_nvc_evidence()
    design = _evidence_design(tmp_path, "wait for 5 ns; std.env.finish;")
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    flow = runner.run_flow(Nvc, design, flow_settings=["wave=true"])
    assert flow is not None and flow.succeeded
    assert (flow.run_path / "dump.fst").stat().st_size > 0
    assert not list(flow.run_path.glob("true*"))


def test_nvc_filters_the_waveform_with_the_globs(tmp_path):
    require_nvc_evidence()
    source = tmp_path / "tb.vhdl"
    source.write_text(
        "entity tb is end; architecture sim of tb is\n"
        "signal keep, drop : integer := 0;\n"
        "begin process begin wait for 5 ns; keep <= 1; drop <= 1; wait for 5 ns;\n"
        "std.env.finish; wait; end process; end;\n"
    )
    design = Design(
        name="globs",
        design_root=tmp_path,
        rtl={"sources": [source], "top": "tb"},
        tb={"top": "tb"},
        language={"vhdl": {"standard": "2008"}},
    )
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    flow = runner.run_flow(
        Nvc,
        design,
        flow_settings=["vcd=f.vcd", "wave_include_glob=*:keep", "wave_exclude_glob=*:drop"],
    )
    assert flow is not None and flow.succeeded
    text = (flow.run_path / "f.vcd").read_text()
    assert " keep " in text and " drop " not in text
