"""The waveform settings of every simulator.

One rule reads them (`dataclass.waveform_value`, applied by `Flow.Settings`): the boolean `true`
is on, `false` is off, and text that reads as a boolean is refused, as for a boolean setting. The
command line reads `-s vcd=true` as the boolean, so it and `vcd: true` are one setting. A
simulator either writes the waveform through its own mechanism or refuses the setting when
planned; none ignores it. The sweeps below classify every waveform setting of every simulator, so a new setting or
simulator fails here until it is classified.
"""

import gzip
from pathlib import Path, PurePath

import pytest
from pydantic import ValidationError

from xeda import Design
from xeda.dataclass import (
    COMPRESSED_FORMATS,
    WAVEFORM_SUFFIXES,
    WaveformText,
    format_of_waveform_name,
    waveform_fields,
    waveform_format,
    waveform_format_of,
    waveform_value,
)
from xeda.flow import FlowSettingsError, FlowSettingsException, SimFlow
from xeda.flow.flow import identity_settings
from xeda.flow_runner import DefaultRunner, get_flow_class
from xeda.flow_runner.settings_layers import merge_layers
from xeda.flows import GhdlSim, Nvc

from .settings_samples import flow_classes, minimal_settings
from .sim_evidence_cases import CASES, SimCase, launch_case
from .test_ghdl import _evidence_design
from .tool_utils import fake_calls, require_ghdl, require_nvc_evidence
from .scoping import node_settings

SIM_FLOWS = [(cls, name) for cls, name in flow_classes() if issubclass(cls, SimFlow)]
FIELDS = [
    pytest.param(cls, name, field, extension, id=f"{name}.{field}")
    for cls, name in SIM_FLOWS
    for field, extension in waveform_fields(cls.Settings).items()
]


def true_name(cls, field: str) -> str:
    """The name `true` stands for: the conventional name of the setting."""
    found = waveform_format(cls.Settings, field)
    assert found is not None
    return found[1]


def name_of_flow(cls) -> str:
    return cls.name


def build(cls, **settings):
    return cls.Settings.from_input({**minimal_settings(cls), **settings})


# ---------------------------------------------------------------------------------------------
# one rule for the text and the boolean
# ---------------------------------------------------------------------------------------------


def test_the_marker_is_easy_to_query_by_field():
    settings = get_flow_class("nvc").Settings
    assert waveform_format_of(settings, "vcd") == "vcd"
    assert waveform_format_of(settings, "fst") == "fst"
    assert waveform_format_of(settings, "wave") is None
    assert waveform_format_of(settings, "timeout") is None
    assert waveform_format_of(settings, "no_such_setting") is None


def test_the_sweep_sees_the_simulators_and_their_waveform_settings():
    assert {name for _, name in SIM_FLOWS} >= {"ghdl_sim", "nvc", "verilator", "vcs", "yosys_sim"}
    assert {param.id for param in FIELDS} >= {
        "ghdl_sim.vcd",
        "ghdl_sim.wave",
        "ghdl_sim.fst",
        "nvc.fst",
        "verilator.saif",
        "vcs.fsdb",
        "vivado_sim.saif",
    }


@pytest.mark.parametrize("cls,name,field,extension", FIELDS)
def test_the_text_true_or_false_is_refused_like_a_boolean_settings(cls, name, field, extension):
    """From Python or a file, a waveform setting takes the boolean `true` or `false`, as a boolean
    setting does, and refuses the text `"true"` or `"false"`."""
    assert getattr(build(cls, **{field: False}), field) is None
    assert getattr(build(cls, **{field: ""}), field) is None
    assert getattr(build(cls, **{field: True}), field) is not None
    for text in ("true", "True", "false", "FALSE"):
        with pytest.raises(FlowSettingsError, match=rf"`{text}` is text, not a boolean: write it"):
            build(cls, **{field: text})
        with pytest.raises(ValidationError, match="not a boolean"):
            setattr(build(cls), field, text)


@pytest.mark.parametrize("cls,name,field,extension", FIELDS)
def test_the_command_line_reads_true_or_false_as_the_boolean(cls, name, field, extension):
    """The command line writes only text, so `-s <setting>=true` is the boolean `true`, for every
    waveform setting of every simulator, and a name stays a name."""
    for text, value in (("true", True), ("True", True), ("false", False), ("w", "w")):
        layer = merge_layers([f"{field}={text}"], settings_cls=cls.Settings)
        assert layer[field] == value and type(layer[field]) is type(value)
    with pytest.raises(FlowSettingsError, match="`yes` is text, not a boolean"):
        build(cls, **merge_layers([f"{field}=yes"], settings_cls=cls.Settings))


@pytest.mark.parametrize("cls,name,field,extension", FIELDS)
def test_the_yaml_boolean_words_are_refused_like_a_boolean_settings(cls, name, field, extension):
    """A setting is on, off or a name, so `yes` is neither: it names the boolean to write, as for
    a boolean setting, and the file name that would take its place."""
    for word in ("yes", "No", "on", "OFF", "y", "n", " true ", "TRUE ", " false"):
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
    assert PurePath(expected.format(design="design")) == conventional
    on = identity_settings(build(cls, **{field: True}), "design")
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
        (False, "vcd", "dump.vcd", None),
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


@pytest.mark.parametrize(
    "value",
    [
        "yes",
        "YES",
        "no",
        "on",
        "off",
        "y",
        "n",
        "t",
        "f",
        " on ",
        " true ",
        "TRUE ",
        " False",
        "\ttrue",
        "true",
        "TRUE",
        "false",
    ],
)
def test_the_rule_refuses_the_words_yaml_1_1_reads_as_booleans(value):
    """Text that reads as a boolean is no boolean and no file name, as for a boolean setting:
    `true`, `on` and `true` with spaces around it."""
    with pytest.raises(WaveformText, match="is text, not a boolean"):
        waveform_value(value, "vcd", "dump.vcd")


def test_true_may_be_a_name_that_holds_the_design_name():
    assert waveform_value(True, "saif", "outputs/{design}.saif") == "outputs/{design}.saif"
    assert waveform_value(False, "saif", "outputs/{design}.saif") is None


def test_the_command_line_text_gives_the_run_a_boolean_waveform_would(tmp_path):
    """A run planned from `-s vcd=true` is the run planned from `vcd: true`: the same settings and
    the same identity."""
    design = _evidence_design(tmp_path, "std.env.finish;")
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    text = runner.plan(
        "ghdl_sim", design, flow_settings=node_settings("ghdl_sim", ["vcd=true"])
    ).node("ghdl_sim")
    boolean = runner.plan(
        "ghdl_sim", design, flow_settings=node_settings("ghdl_sim", {"vcd": True})
    ).node("ghdl_sim")
    assert text.settings.vcd == "dump.vcd"
    assert text.settings.model_dump() == boolean.settings.model_dump()
    assert text.flowrun_hash == boolean.flowrun_hash


# ---------------------------------------------------------------------------------------------
# no waveform setting is ignored
# ---------------------------------------------------------------------------------------------

#: What each simulator hands its tool for a waveform setting set to `true`: `(where, needle)`.
#: `where` names the command or script that must carry the needle, so a flag that another
#: command takes does not count: `("tool", name, word)` is the arguments of the commands of tool
#: `name` that have the argument `word` (any when None), `("script", file)` a script xeda wrote,
#: `("tcl",)` a command a fake Tcl tool recorded, and `("last",)` the last command that ran.
#: A setting that a simulator cannot honor is not here: it is `unsupported_waveforms`, refused
#: when planned.
HONORED: dict[tuple[str, str | None, str], list[tuple[tuple, str]]] = {
    ("ghdl_sim", None, "vcd"): [(("tool", "ghdl", "run"), "--vcd=dump.vcd")],
    ("ghdl_sim", None, "vcdgz"): [(("tool", "ghdl", "run"), "--vcdgz=dump.vcd.gz")],
    ("ghdl_sim", None, "fst"): [(("tool", "ghdl", "run"), "--fst=dump.fst")],
    ("ghdl_sim", None, "wave"): [(("tool", "ghdl", "run"), "--wave=dump.ghw")],
    ("nvc", None, "vcd"): [
        (("tool", "nvc", "-r"), "--wave=dump.vcd"),
        (("tool", "nvc", "-r"), "--format=vcd"),
    ],
    ("nvc", None, "fst"): [(("tool", "nvc", "-r"), "--wave=dump.fst")],
    ("modelsim", None, "vcd"): [(("tcl",), "vcd file dump.vcd"), (("tcl",), "vcd add -r")],
    ("vcs", None, "fsdb"): [(("script", "vcs_runtime.tcl"), 'dump -file "dump.fsdb" -type FSDB')],
    ("vcs", None, "vpd"): [(("script", "vcs_runtime.tcl"), 'dump -file "dump.vpd" -type vpd')],
    ("vcs", None, "evcd"): [(("script", "vcs_runtime.tcl"), 'dump -file "dump.evcd" -type evcd')],
    ("vivado_sim", None, "vcd"): [(("tcl",), "open_vcd dump.vcd")],
    ("vivado_sim", None, "saif"): [(("tcl",), "open_saif outputs/oracle.saif")],
    ("vivado_postsynth_sim", None, "vcd"): [(("tcl",), "open_vcd dump.vcd")],
    ("vivado_postsynth_sim", None, "saif"): [(("tcl",), "open_saif outputs/oracle.saif")],
    ("verilator", None, "vcd"): [(("tool", "verilator", None), "--trace-vcd")],
    ("verilator", None, "fst"): [(("tool", "verilator", None), "--trace-fst")],
    ("verilator", None, "saif"): [(("tool", "verilator", None), "--trace-saif")],
    ("bsc_sim", "bluesim", "vcd"): [(("last",), "-V"), (("last",), "dump.vcd")],
    ("bsc_sim", "verilator", "vcd"): [(("last",), "+bscvcd")],
    ("bsc_sim", "modelsim", "vcd"): [(("script", "bsc_modelsim_runtime.tcl"), "+bscvcd=dump.vcd")],
    ("bsc_sim", "questa", "vcd"): [(("script", "bsc_modelsim_runtime.tcl"), "+bscvcd=dump.vcd")],
    ("bsc_sim", "xsim", "vcd"): [(("tool", "xsim", None), "+bscvcd=dump.vcd")],
}
#: bsc's Verilog drivers take the name as a plusarg of the simulation command
for _backend in ("iverilog", "vcs", "vcsi"):
    HONORED[("bsc_sim", _backend, "vcd")] = [(("last",), "+bscvcd=dump.vcd")]


def handed(where: tuple, needle: str, flow, calls) -> bool:
    """Whether the command or script `where` names carries `needle`."""
    kind = where[0]
    if kind == "tool":
        _, tool, word = where
        words = [
            word_
            for executable, args in calls
            if Path(executable).name == tool and (word is None or word in args)
            for word_ in args
        ]
        return needle in words
    if kind == "script":
        script = flow.run_path / where[1]
        return script.is_file() and needle in script.read_text(encoding="utf-8")
    if kind == "tcl":
        try:
            return any(" ".join(call).startswith(needle) for call in fake_calls(flow.run_path))
        except FileNotFoundError:
            return False
    assert kind == "last"
    return needle in calls[-1][1]


def waveform_cases():
    for case in CASES:
        cls = get_flow_class(case.flow)
        for field in waveform_fields(cls.Settings):
            yield pytest.param(case, field, id=f"{case.name}.{field}")


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
            launch_case(case, tmp_path, monkeypatch, settings_overrides={field: True})
        assert f"cannot write a waveform from `{field}`" in str(raised.value)
        return
    calls: list = []
    given = True
    flow = launch_case(
        case, tmp_path, monkeypatch, settings_overrides={field: given}, calls_out=calls
    )
    for where, needle in HONORED[key]:
        assert handed(where, needle, flow, calls), (
            f"{case.name} was given `{field}: {given}` and never handed {needle!r} to {where}: "
            f"{calls}"
        )


def run_text(flow, calls) -> tuple[set[str], str]:
    """Everything the tool is handed: the arguments of every command, and the text of the
    scripts xeda wrote (including the commands a fake Tcl tool recorded)."""
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
            "vcd": True,
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
        settings_overrides={"vpd": True, "vpd_size_limit": 4},
        calls_out=calls,
    )
    words = {word for _, args in calls for word in args}
    assert "+vpdfilesize+4" in words
    assert not [word for word in words if word.startswith("+fsdb")]


def test_a_refused_waveform_is_refused_when_planned_and_starts_no_tool(tmp_path):
    design = Design(
        name="d",
        root=tmp_path,
        rtl={"sources": [_write(tmp_path / "d.v", "module d; endmodule\n")], "top": "d"},
    )
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    with pytest.raises(FlowSettingsException, match=r"vcs cannot write a waveform from `vcd`.*vpd"):
        runner.plan("vcs", design, flow_settings=node_settings("vcs", ["vcd=true"]))
    with pytest.raises(FlowSettingsException, match=r"yosys_sim cannot write.*`vcd`.*vcd_writer"):
        runner.plan("yosys_sim", design, flow_settings=node_settings("yosys_sim", ["vcd=x.vcd"]))
    assert not (tmp_path / "run").exists()


def _write(path: Path, text: str) -> Path:
    path.write_text(text)
    return path


@pytest.mark.parametrize("cls,name,field,extension", FIELDS)
def test_a_name_never_selects_another_format(cls, name, field, extension):
    """The setting a name is given to decides the format. A name that ends in an extension of
    another format is refused, whether or not this simulator has a setting for that format: a
    location is written under the conventional name of this setting, and `true` and a location
    of another format would otherwise be one run. The advice names the setting of the other
    format when this simulator has one that works, and never one that it refuses."""
    usable = {
        other: other_field
        for other_field, other in waveform_fields(cls.Settings).items()
        if other_field not in cls.unsupported_waveforms
    }
    for other, suffixes in WAVEFORM_SUFFIXES.items():
        if other == extension:
            continue
        for suffix in suffixes:
            for given in (f"w.{suffix}", f"/elsewhere/w.{suffix.upper()}"):
                with pytest.raises(FlowSettingsException) as raised:
                    cls.check_settings_supported(build(cls, **{field: given}))
                message = str(raised.value)
                if field in cls.unsupported_waveforms:
                    assert "cannot write a waveform" in message
                    continue
                assert f"ends in .{suffix}" in message
                if other in usable:
                    assert f"give the name to `{usable[other]}`" in message
                elif COMPRESSED_FORMATS.get(other) == extension:
                    assert f"{name_of_flow(cls)} writes plain {extension.upper()}" in message
                else:
                    assert f"use a .{extension} name" in message
                for refused in cls.unsupported_waveforms:
                    assert f"`{refused}`," not in message.split("ends in")[1]


@pytest.mark.parametrize(
    "flow,field,given,advice",
    [
        ("modelsim", "vcd", "x.fst", "use a .vcd name"),
        ("modelsim", "vcd", "x.ghw", "use a .vcd name"),
        ("modelsim", "vcd", "x.vpd", "use a .vcd name"),
        ("modelsim", "vcd", "x.saif", "use a .vcd name"),
        ("vcs", "vpd", "x.vcd", "use a .vpd name"),
        ("ghdl_sim", "wave", "x.vcd", "give the name to `vcd`"),
        (
            "ghdl_sim",
            "vcd",
            "x.vcd.gz",
            "give the name to `vcdgz`, which writes gzip-compressed VCD data",
        ),
        ("ghdl_sim", "vcd", "W.VCD.GZ", "give the name to `vcdgz`"),
        ("ghdl_sim", "vcd", "x.vcdgz", "give the name to `vcdgz`"),
        ("ghdl_sim", "vcd", "x.gz", "give the name to `vcdgz`"),
        ("ghdl_sim", "vcd", "/dir/w.vcd.gz", "give the name to `vcdgz`"),
        ("ghdl_sim", "vcdgz", "x.vcd", "give the name to `vcd`"),
        ("ghdl_sim", "vcdgz", "x.fst.gz", "uncompressed"),
        ("ghdl_sim", "fst", "x.fst.gz", "uncompressed"),
        ("nvc", "vcd", "w.vcdgz", "writes plain VCD data: drop the gzip ending"),
        ("nvc", "vcd", "w.vcd.gz", "writes plain VCD data: drop the gzip ending"),
        ("nvc", "vcd", "w.gz", "writes plain VCD data: drop the gzip ending"),
        ("modelsim", "vcd", "x.vcd.gz", "writes plain VCD data: drop the gzip ending"),
        ("verilator", "vcd", "x.vcdgz", "writes plain VCD data: drop the gzip ending"),
        ("vivado_sim", "vcd", "/d/x.vcd.gz", "writes plain VCD data: drop the gzip ending"),
        ("bsc_sim", "vcd", "x.GZ", "writes plain VCD data: drop the gzip ending"),
        ("nvc", "fst", "x.fst.gz", "uncompressed"),
        ("nvc", "vcd", "x.fst", "give the name to `fst`"),
    ],
)
def test_some_refusals_by_name(flow, field, given, advice):
    cls = get_flow_class(flow)
    with pytest.raises(FlowSettingsException, match=advice):
        cls.check_settings_supported(build(cls, **{field: given}))


@pytest.mark.parametrize("cls,name,field,extension", FIELDS)
def test_a_setting_takes_every_extension_of_its_own_format(cls, name, field, extension):
    if field in cls.unsupported_waveforms:
        pytest.skip("refused as a whole")
    for suffix in WAVEFORM_SUFFIXES[extension]:
        cls.check_settings_supported(build(cls, **{field: f"w.{suffix}"}))


def test_every_waveform_format_has_its_extensions_in_the_one_table():
    formats = {
        extension for cls, _ in SIM_FLOWS for extension in waveform_fields(cls.Settings).values()
    }
    assert formats <= set(WAVEFORM_SUFFIXES), formats - set(WAVEFORM_SUFFIXES)
    for extension, suffixes in WAVEFORM_SUFFIXES.items():
        assert extension in suffixes


@pytest.mark.parametrize(
    "name,expected",
    [
        ("w.vcd", ("vcd", "vcd")),
        ("W.VCD", ("vcd", "vcd")),
        ("d/w.fst", ("fst", "fst")),
        ("w.gz", ("vcdgz", "gz")),
        ("w.vcd.gz", ("vcdgz", "vcd.gz")),
        ("w.vcdgz", ("vcdgz", "vcdgz")),
        ("vcd", None),
        ("w.vcdx", None),
        ("w.out", None),
        (3, None),
    ],
)
def test_the_format_of_a_waveform_name(name, expected):
    assert format_of_waveform_name(name) == expected


#: The waveform settings that are not named after their format. A setting is named after its
#: format (`vcd`, `fst`, `saif`, `vpd`, ...), so the same name means the same thing on every
#: simulator. A tool-native format that no other simulator writes may have a name of the tool.
NATIVE_NAMES = {
    ("ghdl_sim", "wave"): "GHDL's own waveform format is GHW, written by its `--wave` option; "
    "no other simulator writes it, so it has no shared setting to match",
}


def test_a_waveform_setting_is_named_after_its_format():
    misnamed = {
        (name, field): extension
        for cls, name in SIM_FLOWS
        for field, extension in waveform_fields(cls.Settings).items()
        if field != extension
    }
    assert set(misnamed) == set(
        NATIVE_NAMES
    ), f"name a waveform setting after its format, or review it in NATIVE_NAMES: {misnamed}"
    assert misnamed[("ghdl_sim", "wave")] == "ghw"


# ---------------------------------------------------------------------------------------------
# settings that refine a waveform need one
# ---------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "flow,settings,match",
    [
        ("nvc", {"wave_arrays": 8}, "wave_arrays. refines the waveform"),
        ("nvc", {"wave_include_glob": "*:x"}, "wave_include_glob. refines the waveform"),
        ("nvc", {"wave_exclude_glob": "*:x"}, "wave_exclude_glob. refines the waveform"),
        ("nvc", {"vcd": True, "fst": True}, "one waveform, and vcd and fst are set"),
        ("nvc", {"fst": "w.vcd"}, r"`fst` writes FST data.*`vcd`"),
        ("nvc", {"vcd": "w.fst"}, r"`vcd` writes VCD data.*`fst`"),
        ("ghdl_sim", {"wave": "w.vcd"}, r"`wave` writes GHW data.*`vcd`"),
        ("ghdl_sim", {"wave": "/elsewhere/w.fst"}, r"`wave` writes GHW data.*`fst`"),
        ("ghdl_sim", {"vcd": "w.fst"}, r"`vcd` writes VCD data.*`fst`"),
        ("ghdl_sim", {"vcd": True, "fst": True}, "one waveform, and vcd, fst are set"),
        ("ghdl_sim", {"wave": True, "fst": True}, "one waveform, and fst, wave are set"),
        ("ghdl_sim", {"read_wave_opt": "w.opt"}, "read_wave_opt. filters the waveform"),
        ("vcs", {"vpd": True, "fsdb": True}, "one waveform, and fsdb, vpd are set"),
        ("vcs", {"to_vcd": True}, "to_vcd. converts an FSDB or VPD waveform"),
        ("vcs", {"to_vcd": True, "evcd": True}, "to_vcd. converts an FSDB or VPD waveform"),
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
        ("nvc", {"fst": "w.fst"}),
        ("nvc", {"fst": "w"}),
        ("nvc", {"vcd": True, "fst": False}),
        ("nvc", {"vcd": True, "wave_include_glob": "*:x"}),
        ("ghdl_sim", {"wave": "w.ghw"}),
        ("ghdl_sim", {"vcd": True, "read_wave_opt": "w.opt"}),
        ("vcs", {"vpd": True, "to_vcd": True, "vpd_size_limit": 4}),
        ("vcs", {"fsdb": True, "to_vcd": True, "fsdb_size_limit": 7}),
        ("vcs", {}),
        ("nvc", {"fst": True, "wave_arrays": 8}),
        ("nvc", {}),
        ("vivado_sim", {"vcd": True, "vcd_scope": "/tb", "vcd_level": 2}),
        ("verilator", {"fst": True, "trace_structs": True}),
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
    flow = runner.run_flow(
        GhdlSim, design, flow_settings=node_settings(GhdlSim, [f"{setting}=true"])
    )
    assert flow is not None and flow.succeeded
    assert (flow.run_path / written).stat().st_size > 0
    assert not list(flow.run_path.glob("true*"))


@pytest.mark.parametrize(
    "value,written",
    [("true", "dump.vcd.gz"), ("w.vcdgz", "w.vcdgz"), ("w.vcd.gz", "w.vcd.gz"), ("w", "w.vcdgz")],
)
def test_ghdl_writes_a_compressed_vcd_for_the_vcdgz_setting(tmp_path, value, written):
    """`vcdgz` always passes `--vcdgz`: the file is gzip, and it holds VCD."""
    require_ghdl()
    design = _evidence_design(tmp_path, "wait for 5 ns; std.env.finish;")
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    flow = runner.run_flow(
        GhdlSim, design, flow_settings=node_settings(GhdlSim, [f"vcdgz={value}"])
    )
    assert flow is not None and flow.succeeded
    data = (flow.run_path / written).read_bytes()
    assert data[:2] == b"\x1f\x8b"
    assert gzip.decompress(data).startswith(b"$date") or b"$var" in gzip.decompress(data)


def test_ghdl_delivers_a_compressed_vcd_to_a_location_as_a_copy(tmp_path):
    require_ghdl()
    design = _evidence_design(tmp_path, "wait for 5 ns; std.env.finish;")
    target = tmp_path / "out" / "waves.vcd.gz"
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    flow = runner.run_flow(
        GhdlSim, design, flow_settings=node_settings(GhdlSim, [f"vcdgz={target}"])
    )
    assert flow is not None and flow.succeeded
    assert (flow.run_path / "dump.vcd.gz").read_bytes()[:2] == b"\x1f\x8b"
    assert b"$var" in gzip.decompress(target.read_bytes())


def test_ghdl_vcd_is_plain_whatever_the_case_of_the_name(tmp_path):
    require_ghdl()
    design = _evidence_design(tmp_path, "wait for 5 ns; std.env.finish;")
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    flow = runner.run_flow(GhdlSim, design, flow_settings=node_settings(GhdlSim, ["vcd=w.VCD"]))
    assert flow is not None and flow.succeeded
    assert b"$var" in (flow.run_path / "w.VCD").read_bytes()
    with pytest.raises(FlowSettingsException, match="give the name to `vcdgz`"):
        runner.plan("ghdl_sim", design, flow_settings=node_settings("ghdl_sim", ["vcd=w.vcd.gz"]))


@pytest.mark.parametrize(
    "setting,value,written,fmt",
    [
        ("vcd", "true", "dump.vcd", "vcd"),
        ("vcd", "waves.vcd", "waves.vcd", "vcd"),
        ("fst", "true", "dump.fst", "fst"),
        ("fst", "w.fst", "w.fst", "fst"),
    ],
)
def test_nvc_writes_the_waveform_through_its_own_option(tmp_path, setting, value, written, fmt):
    """`vcd` is `--wave=<file> --format=vcd`; `fst` is `--wave=<file>`, nvc's own FST file."""
    require_nvc_evidence()
    design = _evidence_design(tmp_path, "wait for 5 ns; std.env.finish;")
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    flow = runner.run_flow(Nvc, design, flow_settings=node_settings(Nvc, [f"{setting}={value}"]))
    assert flow is not None and flow.succeeded
    wave = flow.run_path / written
    assert wave.stat().st_size > 0
    head = wave.read_bytes()[:16]
    assert (b"$date" in head or b"$version" in head or b"$timescale" in head) == (fmt == "vcd")
    assert not list(flow.run_path.glob("true*"))


@pytest.mark.parametrize(
    "removed,replacement", [("wave", "`fst`"), ("wave_format", "`fst` or `vcd`")]
)
def test_nvc_wave_and_wave_format_were_removed(removed, replacement):
    with pytest.raises(FlowSettingsError, match=rf"`{removed}` was removed: use {replacement}"):
        build(Nvc, **{removed: "fst"})


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
        root=tmp_path,
        rtl={"sources": [source], "top": "tb"},
        tb={"top": "tb"},
        language={"vhdl": {"standard": "2008"}},
    )
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    flow = runner.run_flow(
        Nvc,
        design,
        flow_settings=node_settings(
            Nvc, ["vcd=f.vcd", "wave_include_glob=*:keep", "wave_exclude_glob=*:drop"]
        ),
    )
    assert flow is not None and flow.succeeded
    text = (flow.run_path / "f.vcd").read_text()
    assert " keep " in text and " drop " not in text


# ---------------------------------------------------------------------------------------------
# a flow built directly, and the plan
# ---------------------------------------------------------------------------------------------


def test_a_flow_built_directly_writes_the_design_name_into_a_template(tmp_path):
    """Only the launcher delivers, but a flow made without it resolves `true` for `saif` to the
    path it writes, by the function the launcher uses."""
    from xeda.flows import VivadoSim

    from .test_vivado import _vivado_sim_design

    design = _vivado_sim_design(tmp_path / "design")
    flow = VivadoSim({"saif": True}, design, tmp_path / "run")
    assert isinstance(flow.settings, VivadoSim.Settings)
    assert flow.settings.saif == Path("outputs/simdesign.saif")
    located = VivadoSim({"saif": str(tmp_path / "x.saif")}, design, tmp_path / "run2")
    assert located.settings.saif == Path(str(tmp_path / "x.saif"))  # delivery fills it in


@pytest.mark.parametrize(
    "flow_class,settings",
    [
        (Nvc, {"vcd": True, "fst": True}),
        (GhdlSim, {"vcd": True, "vcdgz": True}),
        (GhdlSim, {"read_wave_opt": "wave.opt"}),
    ],
)
def test_a_flow_built_directly_refuses_what_planning_refuses(tmp_path, flow_class, settings):
    design = _evidence_design(tmp_path, "std.env.finish;")
    flow = flow_class(settings, design, tmp_path / "run")
    with pytest.raises(FlowSettingsException):
        flow.init()


def test_saif_true_a_location_and_the_power_demand_are_one_run(tmp_path):
    from .test_tool_input_equivalence import VIVADO_SETTINGS, write_vivado_design

    write_vivado_design(tmp_path)
    runner = DefaultRunner(tmp_path / "run", display_results=False)

    def simulation(*extra: str):
        plan = runner.plan(
            "vivado_power",
            tmp_path / "design.yaml",
            flow_settings=node_settings("vivado_power", [*VIVADO_SETTINGS, *extra]),
        )
        return plan.node("vivado_postsynth_sim")

    demand = simulation()
    on = simulation("flows.vivado_postsynth_sim.saif=true")
    located = simulation(f"flows.vivado_postsynth_sim.saif={tmp_path}/elsewhere/a.saif")
    assert demand.flowrun_hash == on.flowrun_hash == located.flowrun_hash
