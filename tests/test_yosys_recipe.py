"""`synth_pass_only` omits Xeda's stages around the synthesis pass.

xeda's default `yosys_fpga` recipe elaborates and optimizes around `synth_<target>` and tells
ABC9 the clock period. ABC9's mapping can depend on those stages. With reader settings and pass
choices matched, `synth_pass_only` allows comparison with the native pass to isolate their effect.

These launches run the process fakes of `tests/fake_tools` (`use_fake_fpga_tools`), so the
oracle runs in CI with no yosys installed. `tests/test_yosys_recipe_real.py` is the other half:
it checks that the netlist really is the pass's own, on the installed yosys.

Going through a whole launch is the point. The decision not to read the target's primitive
libraries early lives in `YosysFpga.run()`, not in the templates, so a render-level helper that
passes `primitive_libraries` itself would never see it.
"""

import json
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda.dataclass import written_role
from xeda.flow import FlowSettingsError
from xeda.flow_runner import DefaultRunner
from xeda.flows import YosysFpga

from . import tool_utils

#: one FPGA per `synth_command` target, so the mode is checked on every pass it can run
#: (Gowin has no part parser, so it is named by vendor/family/device)
PARTS: dict[str, "str | dict[str, str]"] = {
    "xilinx": "xc7a100tcsg324-1",
    "ecp5": "LFE5U-25F-6BG381C",
    "ice40": "iCE40HX1K-TQ144",
    "nexus": "LIFCL-40-9BG400C",
    "gowin": {"vendor": "gowin", "family": "gowin", "device": "GW1N-9"},
}
BY_TARGET = pytest.mark.parametrize("target", sorted(PARTS), ids=sorted(PARTS))
BY_FORMAT = pytest.mark.parametrize("script_format", ["ys", "tcl"])

#: What the mode needs written besides `synth_pass_only`: it reads the sources as a bare
#: `yosys <files>` does, so xeda's own reader choices (`-sv` on a `.v` source, and the slang
#: plugin for a `.sv` one) are refused and the native ones are written out.
PASS_ONLY_READER: dict[str, Any] = {"read_verilog_flags": [], "systemverilog": "default"}

#: Commands that only write to the log. They are the only ones that cannot touch the design, so
#: the shape assertions drop them rather than pin their prose.
LOGGING = {"logger", "log", "echo"}

#: Every command xeda renders that `yosys -p 'synth_<target>' <sources>` does not. Each is
#: either omitted under `synth_pass_only` or refused outright; the plan enumerates them with the
#: cell counts each one moves. `scratchpad` covers both ABC9 tweaks (`flow3`, the target delay).
DEVIATIONS = {"hierarchy", "check", "proc", "flatten", "opt_clean", "opt", "scratchpad", "prep"}


@pytest.fixture
def toolchain(tmp_path, monkeypatch):
    tool_utils.use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")
    monkeypatch.chdir(tmp_path)


def _design(tmp_path: Path) -> Design:
    root = tmp_path / "design"
    root.mkdir(exist_ok=True)
    (root / "top.v").write_text(
        "module leaf(input a, output y); assign y = ~a; endmodule\n"
        "module top(input clk, input a, output q);\n"
        "  wire n; leaf u(.a(a), .y(n));\n"
        "  reg r; always @(posedge clk) r <= n; assign q = r;\n"
        "endmodule\n"
    )
    return Design(
        name="top",
        design_root=root,
        rtl={"sources": ["top.v"], "top": "top", "clock": {"port": "clk"}},
    )


def _launch(tmp_path: Path, part: "str | dict[str, str]", **settings) -> Path:
    """Launch `yosys_fpga` on the fakes and return its run directory."""
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    if settings.get("synth_pass_only"):
        settings = {**PASS_ONLY_READER, **settings}
    flow = runner.run(
        YosysFpga,
        _design(tmp_path),
        flow_settings={"fpga": part, "clock": {"period": 5.0}, **settings},
    )
    assert flow is not None and flow.succeeded, "the fakes must complete, or nothing is rendered"
    return Path(flow.run_path)


def _commands(run_path: Path, script_format: str) -> list[str]:
    """The ordered yosys commands of the rendered script, logging dropped.

    A `.tcl` script writes each command as `yosys <command ...>`; `tee` is a bare word in both
    formats, and `puts`/`yosys -import` are TCL's own, not commands on the design.
    """
    script = run_path / f"yosys_fpga_synth.{script_format}"
    commands = []
    for line in script.read_text().splitlines():
        words = line.split()
        if not words or words[0] == "puts":
            continue
        if script_format == "tcl" and words[0] == "yosys":
            words = words[1:]
        if not words or words[0] == "-import" or words[0] in LOGGING:
            continue
        commands.append(words[0])
    return commands


# --------------------------------------------------------------- the mode's script, exactly


@BY_TARGET
@BY_FORMAT
def test_synth_pass_only_renders_the_pass_and_nothing_but_the_pass(
    tmp_path, toolchain, target, script_format
):
    """The whole shape, not merely the absence of the pre-passes.

    Every `read_verilog` counts: each one advances yosys's `autoidx`, which renumbers the
    design's generated cell names, and ABC9 maps by those names -- so one extra library read is
    enough to change the netlist by a cell. The reads here are the design's own source and
    nothing else, exactly as the command line would read it.
    """
    run_path = _launch(tmp_path, PARTS[target], synth_pass_only=True, script_format=script_format)
    synth = {
        "xilinx": "synth_xilinx",
        "ecp5": "synth_ecp5",
        "ice40": "synth_ice40",
        "nexus": "synth_lattice",
        "gowin": "synth_gowin",
    }[target]
    assert _commands(run_path, script_format) == [
        "read_verilog",  # the design's source, deferred, as `yosys <file>` reads it
        synth,  # the pass, which elaborates, flattens, optimizes and maps on its own
        "tee",  # `check`: read-only
        "tee",  # `stat`: read-only
        "write_json",
        "write_verilog",
    ]


@BY_TARGET
@BY_FORMAT
def test_the_default_recipe_still_runs_every_step_xeda_adds(
    tmp_path, toolchain, target, script_format
):
    """The teeth: a mode that quietly did nothing would pass the test above on its own."""
    run_path = _launch(tmp_path, PARTS[target], script_format=script_format)
    commands = _commands(run_path, script_format)
    assert set(commands) >= {"hierarchy", "check", "proc", "opt_clean", "scratchpad"}
    # the libraries the pass reads in its `begin` step, read early so xeda's own passes resolve
    # the primitives
    reads = commands.count("read_verilog")
    assert reads == 1 + len(_libraries(PARTS[target]))


def _flattening(run_path: Path, script_format: str) -> dict[str, bool]:
    """Where the rendered script flattens: `prep -flatten` or a `flatten` command (xeda's own
    steps), and `-flatten` on the target's pass."""
    lines = (run_path / f"yosys_fpga_synth.{script_format}").read_text().splitlines()
    words = [line.split()[1:] if line.startswith("yosys ") else line.split() for line in lines]
    pass_line = next(w for w in words if w[:1] and w[0].startswith("synth_"))
    return {
        "xeda": any(w[:1] == ["flatten"] or (w[:1] == ["prep"] and "-flatten" in w) for w in words),
        "pass": "-flatten" in pass_line,
        "pass_off": "-noflatten" in pass_line,
    }


@BY_FORMAT
@pytest.mark.parametrize(
    "flatten,pass_only,expected",
    [
        (None, False, {"xeda": True, "pass": True, "pass_off": False}),
        (True, False, {"xeda": True, "pass": True, "pass_off": False}),
        (False, False, {"xeda": False, "pass": False, "pass_off": False}),
        # the mode's rule is the pass's own defaults: `synth_xilinx` keeps the hierarchy
        (None, True, {"xeda": False, "pass": False, "pass_off": False}),
        (True, True, {"xeda": False, "pass": True, "pass_off": False}),
        (False, True, {"xeda": False, "pass": False, "pass_off": False}),
    ],
)
def test_xilinx_flattens_by_default_except_under_synth_pass_only(
    tmp_path, toolchain, script_format, flatten, pass_only, expected
):
    """The measured better default: `synth_xilinx` alone keeps the hierarchy, and xeda's recipe
    flattens it unless told not to. An explicit value is honored in both modes."""
    settings: dict[str, Any] = {"synth_pass_only": pass_only}
    if flatten is not None:
        settings["flatten"] = flatten
    run_path = _launch(tmp_path, PARTS["xilinx"], script_format=script_format, **settings)
    assert _flattening(run_path, script_format) == expected


@BY_FORMAT
def test_an_unset_flatten_on_xilinx_renders_exactly_the_script_of_flatten_true(
    tmp_path, toolchain, script_format
):
    """The configuration measured as the better default is `flatten: true`, which flattens in
    xeda's own step, before the RTL outputs, and again with `-flatten` on the pass. An unset
    `flatten` is that run, not a flatter one or a sparser one."""
    name = f"yosys_fpga_synth.{script_format}"
    unset = (_launch(tmp_path, PARTS["xilinx"], script_format=script_format) / name).read_text()
    flat = (
        _launch(tmp_path, PARTS["xilinx"], script_format=script_format, flatten=True) / name
    ).read_text()
    kept = (
        _launch(tmp_path, PARTS["xilinx"], script_format=script_format, flatten=False) / name
    ).read_text()
    assert unset == flat
    assert unset != kept


@BY_FORMAT
@pytest.mark.parametrize("target", ["ecp5", "ice40", "nexus", "gowin"])
def test_the_other_targets_leave_flattening_to_their_pass_by_default(
    tmp_path, toolchain, script_format, target
):
    """Those passes flatten on their own, so an unset `flatten` adds nothing of xeda's."""
    run_path = _launch(tmp_path, PARTS[target], script_format=script_format)
    assert _flattening(run_path, script_format) == {"xeda": False, "pass": False, "pass_off": False}


def _share(tmp_path: Path) -> Path:
    """The fake yosys's data directory, which its `+/` and `yosys-config --datdir` name."""
    return tmp_path / "toolchain" / "share" / "yosys"


def _spellings(tmp_path: Path, library: str) -> dict[str, str]:
    """One installed library, named every way a `verilog_lib` entry could name it."""
    relative = library.removeprefix("+/")
    folder, name = relative.rsplit("/", 1)
    installed = _share(tmp_path) / relative
    other = tmp_path / "elsewhere" / folder
    other.mkdir(parents=True, exist_ok=True)
    link = other / f"linked_{name}"
    link.symlink_to(installed)
    hard = other / f"hard_{name}"
    hard.hardlink_to(installed)
    return {
        "yosys": library,
        "yosys-roundabout": f"+/{folder}/../{folder}/{name}",
        "absolute": str(installed),
        "absolute-roundabout": str(installed.parent / ".." / folder.split("/")[-1] / name),
        "symbolic-link": str(link),
        "hard-link": str(hard),
    }


SPELLINGS = [
    "yosys",
    "yosys-roundabout",
    "absolute",
    "absolute-roundabout",
    "symbolic-link",
    "hard-link",
]


@BY_TARGET
@BY_FORMAT
@pytest.mark.parametrize("spelling", SPELLINGS)
@pytest.mark.parametrize("mode", [True, False], ids=["mode", "default"])
def test_a_verilog_lib_naming_the_passs_own_library_adds_no_read_however_it_is_spelled(
    tmp_path, toolchain, target, script_format, spelling, mode
):
    """`verilog_lib` must not let a library the pass reads itself back in by another name.

    The entry is skipped in either recipe, by *which file it is* (`verilog_libraries_to_read`):
    `+/xilinx/cells_sim.v`, the same file by its absolute path, through a symbolic or a hard
    link, or spelled round about, is one library -- otherwise a configuration carrying it would
    read the library once more, and one read is enough to move every generated cell name.
    """
    libraries = _libraries(PARTS[target])
    assert libraries
    run_path = _launch(
        tmp_path,
        PARTS[target],
        synth_pass_only=mode,
        script_format=script_format,
        verilog_lib=[_spellings(tmp_path, library.path)[spelling] for library in libraries],
    )
    reads = _commands(run_path, script_format).count("read_verilog")
    # the design's own source; the default recipe also reads the pass's libraries itself
    assert reads == (1 if mode else 1 + len(libraries))


@BY_FORMAT
def test_a_verilog_lib_that_is_not_the_passs_library_is_still_read(
    tmp_path, toolchain, script_format
):
    """The teeth: the filter is by file identity, so another file of the same content or name
    is the user's own library and stays read, after the sources."""
    share = _share(tmp_path)
    copy = tmp_path / "copy" / "cells_sim.v"
    copy.parent.mkdir()
    copy.write_text((share / "xilinx" / "cells_sim.v").read_text())
    own = tmp_path / "own.v"
    own.write_text("module own(); endmodule\n")
    run_path = _launch(
        tmp_path,
        PARTS["xilinx"],
        synth_pass_only=True,
        script_format=script_format,
        verilog_lib=[str(copy), str(own)],
    )
    assert _commands(run_path, script_format).count("read_verilog") == 3


def test_no_data_directory_is_asked_for_unless_an_entry_needs_one(tmp_path, toolchain, monkeypatch):
    """`yosys-config` runs only to compare an ordinary path with the pass's libraries: a
    `+/` entry is yosys' own spelling, so it needs no data directory."""

    def asked(*_):
        raise AssertionError("yosys' data directory was asked for")

    monkeypatch.setattr("xeda.flows.yosys.yosys_fpga.yosys_data_dir", asked)
    _launch(tmp_path, PARTS["xilinx"], synth_pass_only=True, verilog_lib=["+/xilinx/cells_sim.v"])


@pytest.mark.parametrize("failure", ["missing", "blank"])
def test_an_unknown_data_directory_is_an_error_naming_the_entry_not_a_guess(
    tmp_path, toolchain, monkeypatch, failure
):
    from xeda.tool import Tool
    from xeda.utils import ExecutableNotFound

    def missing(*_):
        raise ExecutableNotFound("yosys-config", "Tool", "", "not found")

    if failure == "missing":
        monkeypatch.setattr("xeda.flows.yosys.yosys_fpga.yosys_data_dir", missing)
    else:  # the real helper, over a `yosys-config` that prints nothing
        monkeypatch.setattr(Tool, "probe_stdout", lambda *_, **__: "\n")
    library = _share(tmp_path) / "xilinx" / "cells_sim.v"
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    with pytest.raises(Exception, match=r"Cannot tell whether `verilog_lib` entry .*cells_sim\.v"):
        runner.run(
            YosysFpga,
            _design(tmp_path),
            flow_settings={
                "fpga": PARTS["xilinx"],
                "clock": {"period": 5.0},
                "synth_pass_only": True,
                **PASS_ONLY_READER,
                "verilog_lib": [str(library)],
            },
        )


def _libraries(part: "str | dict[str, str]") -> list:
    settings = YosysFpga.Settings(fpga=part, clock={"period": 5.0})  # type: ignore[arg-type]
    return settings.primitive_libraries()


@BY_TARGET
def test_synth_pass_only_reads_no_primitive_library_but_still_says_which_the_pass_reads(
    tmp_path, toolchain, target
):
    """`primitive_libraries()` keeps describing the pass's `begin` step either way.

    `tests/test_yosys_templates.py` compares what it returns with the installed yosys's own
    `begin`, so the mode must not weaken it to an empty list; only the script omits the read.
    """
    part = PARTS[target]
    settings = YosysFpga.Settings(fpga=part, clock={"period": 5.0}, synth_pass_only=True)  # type: ignore[arg-type]
    assert settings.primitive_libraries() == _libraries(part)
    script = (_launch(tmp_path, part, synth_pass_only=True) / "yosys_fpga_synth.ys").read_text()
    reads = [line for line in script.splitlines() if line.startswith("read_verilog")]
    assert not [line for line in reads if " +/" in line], reads


@BY_FORMAT
def test_synth_pass_only_leaves_abc9_the_script_and_the_delay_the_pass_gives_it(
    tmp_path, toolchain, script_format
):
    """No `scratchpad`: not `flow3`, and not the clock period as ABC9's target delay.

    A constrained clock is a timing constraint, not a request for a scratchpad tweak, so it is
    not an error -- pass-only mode simply does not supply the implicit ABC9 delay.
    """
    run_path = _launch(tmp_path, PARTS["xilinx"], synth_pass_only=True, script_format=script_format)
    # on the commands, not on the script's text: a source path may spell any word at all
    assert "scratchpad" not in _commands(run_path, script_format)
    default = _launch(
        tmp_path, PARTS["xilinx"], script_format=script_format, netlist_json="other.json"
    )
    text = (default / f"yosys_fpga_synth.{script_format}").read_text()
    assert "scratchpad -copy abc9.script.flow3 abc9.script" in text
    assert "scratchpad -set abc9.D " in text


def test_abc9_scratchpad_is_the_one_place_the_tweaks_are_decided():
    """A pure method, like `synth_command`, so the two script formats cannot disagree."""

    def settings(**kw):
        return YosysFpga.Settings(fpga=PARTS["xilinx"], clock={"period": 5.0}, **kw)  # type: ignore[arg-type]

    flow3 = "scratchpad -copy abc9.script.flow3 abc9.script"
    delay = "scratchpad -set abc9.D 3333.333333333334"
    assert settings().abc9_scratchpad() == [flow3, delay]
    assert settings(flow3=True).abc9_scratchpad() == [flow3, delay]
    assert settings(flow3=False).abc9_scratchpad() == [delay]
    assert settings(abc9=False).abc9_scratchpad() == []
    assert settings(synth_pass_only=True).abc9_scratchpad() == []
    # unset is xeda's own recipe using flow3 -- the behavior `flow3 = True` had
    assert YosysFpga.Settings.model_fields["flow3"].get_default() is None
    no_clock = YosysFpga.Settings(fpga=PARTS["xilinx"])  # type: ignore[arg-type]
    assert no_clock.abc9_scratchpad() == [flow3]


# ----------------------------------------------------------------------------- the refusals


#: setting -> a value that asks for the step `synth_pass_only` does not run
CONFLICTS = {
    "prep": ["-flatten"],
    "pre_synth_opt": True,
    "post_synth_opt": True,
    "splitnets": True,
    "post_synth_rename": ["-hide"],
    "black_box": ["leaf"],
    "keep_hierarchy": ["leaf"],
    "set_attribute": {"keep": {"top": "true"}},
    "set_mod_attribute": {"keep_hierarchy": {"leaf": 1}},
    "clockgate_map": "clock_gates.v",
    "rmports": True,
    "stop_after": "rtl",
    "rtl_json": "rtl.json",
    "rtl_verilog": "rtl.v",
    "rtl_graph": "rtl.dot",
    # reader choices `yosys <files>` does not make; the defaults (`-sv`, slang) are two (below)
    "read_verilog_flags": ["-noautowire"],
    "systemverilog": "slang",
    "read_systemverilog_flags": ["-x"],
    # `Yosys.init` turns `flatten` on for these, which would add `-flatten` to the pass
    "sta": True,
    "ltp": True,
}


@pytest.mark.parametrize("setting", sorted(CONFLICTS), ids=sorted(CONFLICTS))
def test_a_setting_synth_pass_only_cannot_honor_is_refused_naming_both(setting):
    settings = YosysFpga.Settings(
        fpga=PARTS["xilinx"],  # type: ignore[arg-type]
        clock={"period": 5.0},
        synth_pass_only=True,
        **{**PASS_ONLY_READER, setting: CONFLICTS[setting]},
    )
    # alone: the reader settings are written out, so nothing but this one is refused
    assert [key for key, _ in settings.synth_pass_only_conflicts()] == [setting]
    with pytest.raises(FlowSettingsError) as raised:
        YosysFpga.check_settings_supported(settings)
    message = str(raised.value)
    assert "`synth_pass_only`" in message, message
    assert f": {setting} " in message, message


def test_every_conflict_is_reported_at_once_and_none_without_the_mode():
    asked = {name: value for name, value in CONFLICTS.items() if name != "stop_after"}
    settings = YosysFpga.Settings(
        fpga=PARTS["xilinx"],  # type: ignore[arg-type]
        clock={"period": 5.0},
        synth_pass_only=True,
        **asked,
    )
    assert len(CONFLICTS) == 20  # the mode's whole conflict list; `stop_after` is reported alone
    conflicts = settings.synth_pass_only_conflicts()
    assert sorted(key for key, _ in conflicts) == sorted(asked)
    settings.synth_pass_only = False
    assert settings.synth_pass_only_conflicts() == []
    YosysFpga.check_settings_supported(settings)  # nothing conflicts without the mode


def test_the_default_reader_flag_is_refused_and_the_message_says_what_to_write():
    """The fix itself: xeda's own `-sv` default is a reader flag `yosys <file>` does not pass.

    Judged by value, so an unset `read_verilog_flags` and one written as its default are the
    same -- and so are the settings a run reloads from its `settings.json`, which writes every
    field. A refusal that depended on whether the setting was *set* would pass the first run and
    fail the reload (or the other way round).
    """
    settings = YosysFpga.Settings(
        fpga=PARTS["xilinx"],  # type: ignore[arg-type]
        clock={"period": 5.0},
        synth_pass_only=True,
    )
    assert settings.read_verilog_flags == ["-sv"]
    with pytest.raises(FlowSettingsError) as raised:
        YosysFpga.check_settings_supported(settings)
    message = str(raised.value)
    assert ": read_verilog_flags " in message, message
    assert "write `read_verilog_flags: []`".lower() in message.lower(), message
    assert "-s read_verilog_flags=" in message, message
    written = YosysFpga.Settings.model_validate(settings.model_dump(mode="json"))
    assert written.synth_pass_only_conflicts() == settings.synth_pass_only_conflicts() != []
    explicit = YosysFpga.Settings(
        fpga=PARTS["xilinx"],  # type: ignore[arg-type]
        clock={"period": 5.0},
        synth_pass_only=True,
        read_verilog_flags=["-sv"],
    )
    assert explicit.synth_pass_only_conflicts() == settings.synth_pass_only_conflicts()


@pytest.mark.parametrize("flags", [["-sv"], ["-noautowire", "-sv"], ["-noautowire"], ["-formal"]])
def test_any_reader_flag_is_refused_under_the_mode_and_none_is_accepted(flags):
    def conflicts(**settings):
        return YosysFpga.Settings(
            fpga=PARTS["xilinx"],  # type: ignore[arg-type]
            clock={"period": 5.0},
            **{**PASS_ONLY_READER, "read_verilog_flags": flags},
            **settings,
        ).synth_pass_only_conflicts()

    assert [key for key, _ in conflicts(synth_pass_only=True)] == ["read_verilog_flags"]
    assert conflicts(synth_pass_only=False) == []  # the full recipe keeps its reader flags
    plain = YosysFpga.Settings(
        fpga=PARTS["xilinx"],  # type: ignore[arg-type]
        clock={"period": 5.0},
        synth_pass_only=True,
        **PASS_ONLY_READER,
    )
    assert plain.synth_pass_only_conflicts() == []
    YosysFpga.check_settings_supported(plain)


def test_the_command_line_spelling_the_message_gives_is_the_empty_list(tmp_path, toolchain):
    """`-s read_verilog_flags=` is `[]`. The text `[]` is no list: it is refused, naming the
    spelling of the empty list, instead of becoming the one flag `[]`."""
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    base = [
        f"fpga.part={PARTS['xilinx']}",
        "clock.period=5.0",
        "synth_pass_only=true",
        "systemverilog=default",
    ]
    runner.plan(YosysFpga, _design(tmp_path), flow_settings=[*base, "read_verilog_flags="])
    with pytest.raises(FlowSettingsError, match="`read_verilog_flags=` for the empty list"):
        runner.plan(YosysFpga, _design(tmp_path), flow_settings=[*base, "read_verilog_flags=[]"])


def _settings(**settings: Any) -> "YosysFpga.Settings":
    return YosysFpga.Settings(
        fpga=PARTS["xilinx"],  # type: ignore[arg-type]
        clock={"period": 5.0},
        **settings,
    )


def test_the_default_front_end_is_refused_and_the_message_says_what_to_write():
    """`systemverilog` defaults to the slang plugin: `read_slang`, which `yosys <file>.sv` is
    not. A value, like `read_verilog_flags`: unset, written as the default and reloaded from
    `settings.json` are one setting."""
    settings = _settings(synth_pass_only=True, read_verilog_flags=[])
    assert settings.systemverilog == "slang"
    ((key, why),) = settings.synth_pass_only_conflicts()
    assert key == "systemverilog"
    assert "read_verilog -sv" in why and "read_slang" in why, why
    assert "systemverilog: default" in why and "-s systemverilog=default" in why, why
    with pytest.raises(FlowSettingsError) as raised:
        YosysFpga.check_settings_supported(settings)
    assert ": systemverilog " in str(raised.value)
    reloaded = YosysFpga.Settings.model_validate(settings.model_dump(mode="json"))
    assert reloaded.synth_pass_only_conflicts() == settings.synth_pass_only_conflicts()
    explicit = _settings(synth_pass_only=True, read_verilog_flags=[], systemverilog="slang")
    assert explicit.synth_pass_only_conflicts() == settings.synth_pass_only_conflicts()


def test_the_unwritten_mode_is_refused_for_both_reader_defaults_at_once():
    settings = _settings(synth_pass_only=True)
    assert [key for key, _ in settings.synth_pass_only_conflicts()] == [
        "read_verilog_flags",
        "systemverilog",
    ]


@pytest.mark.parametrize("front_end", ["slang", "uhdm"])
def test_a_front_end_that_is_not_yosys_own_is_refused_under_the_mode_only(front_end):
    settings = _settings(synth_pass_only=True, read_verilog_flags=[], systemverilog=front_end)
    assert [key for key, _ in settings.synth_pass_only_conflicts()] == ["systemverilog"]
    assert front_end in settings.synth_pass_only_conflicts()[0][1] or front_end == "uhdm"
    off = _settings(
        read_verilog_flags=["-noautowire"], systemverilog=front_end, read_systemverilog_flags=["-x"]
    )
    assert off.synth_pass_only_conflicts() == []  # the full recipe keeps its readers
    YosysFpga.check_settings_supported(off)


def test_read_systemverilog_flags_are_refused_when_nonempty_and_accepted_when_empty():
    settings = _settings(synth_pass_only=True, **PASS_ONLY_READER, read_systemverilog_flags=["-x"])
    ((key, why),) = settings.synth_pass_only_conflicts()
    assert key == "read_systemverilog_flags"
    assert "read_systemverilog_flags: []" in why and "-s read_systemverilog_flags=" in why, why
    settings.read_systemverilog_flags = []  # an assignment is judged as a construction is
    assert settings.synth_pass_only_conflicts() == []
    settings.systemverilog = "slang"
    assert [key for key, _ in settings.synth_pass_only_conflicts()] == ["systemverilog"]
    settings.systemverilog = "default"
    YosysFpga.check_settings_supported(settings)


def test_the_mode_with_the_native_reader_written_out_is_accepted(tmp_path, toolchain):
    """`systemverilog=default` and `read_verilog_flags=` are all the mode asks, on the command
    line as in a design file; `slang` or a flag spelled `[]` is not."""
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    base = [f"fpga.part={PARTS['xilinx']}", "clock.period=5.0", "synth_pass_only=true"]
    runner.plan(
        YosysFpga,
        _design(tmp_path),
        flow_settings=[*base, "read_verilog_flags=", "systemverilog=default"],
    )
    runner.plan(
        YosysFpga,
        _design(tmp_path),
        flow_settings=[
            *base,
            "read_verilog_flags=",
            "systemverilog=default",
            "read_systemverilog_flags=",
        ],
    )
    for rest in (["read_verilog_flags="], ["read_verilog_flags=", "systemverilog=slang"]):
        with pytest.raises(FlowSettingsError, match="systemverilog"):
            runner.plan(YosysFpga, _design(tmp_path), flow_settings=[*base, *rest])


@BY_FORMAT
def test_the_full_recipe_renders_its_reader_flags_exactly_as_before(
    tmp_path, toolchain, script_format
):
    """The mode off: `read_verilog_flags` is rendered as it always was, default or given."""
    for flags, shown in ((None, "-sv"), (["-noautowire", "-sv"], "-noautowire -sv")):
        settings = {} if flags is None else {"read_verilog_flags": flags}
        run_path = _launch(tmp_path, PARTS["xilinx"], script_format=script_format, **settings)
        text = (run_path / f"yosys_fpga_synth.{script_format}").read_text()
        (line,) = [ln for ln in text.splitlines() if "read_verilog -defer" in ln]
        assert f"read_verilog -defer {shown} " in " ".join(line.split()), line


@BY_FORMAT
def test_a_systemverilog_source_still_reads_as_read_verilog_sv_under_the_mode(
    tmp_path, toolchain, script_format
):
    """The `-sv` on a `.sv` source is the template's own, not `read_verilog_flags`': an empty
    list leaves `read_verilog -sv`, which is what yosys's own front end does for a `.sv` file.
    (Only the built-in front end renders it; the default `slang` renders `read_slang`.)"""
    root = tmp_path / "design"
    root.mkdir(exist_ok=True)
    (root / "top.sv").write_text(
        "module top(input logic clk, output logic q); assign q = clk; endmodule\n"
    )
    design = Design(
        name="top",
        design_root=root,
        rtl={"sources": ["top.sv"], "top": "top", "clock": {"port": "clk"}},
    )
    flow = DefaultRunner(tmp_path / "run", display_results=False).run(
        YosysFpga,
        design,
        flow_settings={
            "fpga": PARTS["xilinx"],
            "clock": {"period": 5.0},
            "synth_pass_only": True,
            "systemverilog": "default",
            "script_format": script_format,
            **PASS_ONLY_READER,
        },
    )
    assert flow is not None and flow.succeeded
    text = (Path(flow.run_path) / f"yosys_fpga_synth.{script_format}").read_text()
    (line,) = [ln for ln in text.splitlines() if "read_verilog" in ln]
    words = line.split()
    after = words[words.index("read_verilog") + 1 :]
    # exactly the template's own `-sv`, then `-defer` and the source: no second `-sv`, no flag
    assert after[:2] == ["-sv", "-defer"] and len(after) == 3, line
    assert after[2].strip('"').endswith("top.sv"), line


@BY_FORMAT
@pytest.mark.parametrize(
    ("name", "declared", "sv"),
    [
        ("top.v", "Verilog", False),
        ("top.sv", "SystemVerilog", True),
        ("top.v", "SystemVerilog", True),
        ("top.sv", "Verilog", False),
    ],
)
def test_the_reader_follows_the_sources_type_not_its_suffix(
    tmp_path, toolchain, script_format, name, declared, sv
):
    """A source's `type` is authoritative (the suffix only infers it), so under the mode a
    SystemVerilog source is read `-sv` and a Verilog one plain, whatever it is called. That is
    `yosys <file>`'s own choice when the type is the suffix's, and the documented difference
    when the design states a contradicting `type` (`docs/flows.rst`, the setting's description)."""
    root = tmp_path / "design"
    root.mkdir(exist_ok=True)
    (root / name).write_text("module top(input clk, output q); assign q = clk; endmodule\n")
    design = Design(
        name="top",
        design_root=root,
        rtl={"sources": [{"file": name, "type": declared}], "top": "top", "clock": {"port": "clk"}},
    )
    flow = DefaultRunner(tmp_path / "run", display_results=False).run(
        YosysFpga,
        design,
        flow_settings={
            "fpga": PARTS["xilinx"],
            "clock": {"period": 5.0},
            "synth_pass_only": True,
            "systemverilog": "default",
            "script_format": script_format,
            **PASS_ONLY_READER,
        },
    )
    assert flow is not None and flow.succeeded
    text = (Path(flow.run_path) / f"yosys_fpga_synth.{script_format}").read_text()
    (line,) = [ln for ln in text.splitlines() if "read_verilog" in ln]
    words = line.split()
    after = words[words.index("read_verilog") + 1 :]
    assert after[:-1] == (["-sv", "-defer"] if sv else ["-defer"]), line
    assert after[-1].strip('"').endswith(name), line


#: every `settings.<name>` that `read_files.ys`/`.tcl` renders into a reader command, with the
#: decision for the mode. "refused": a conflict (and so in `CONFLICTS`); otherwise the reason it
#: is kept. A new setting in either template fails the sweep until it is decided here.
READER_SETTINGS = {
    "read_verilog_flags": "refused",
    "black_box": "refused",
    "clockgate_map": "refused",
    "set_attribute": "refused",
    "set_mod_attribute": "refused",
    "verilog_lib": "a design input: `read_verilog -lib` is how the sources reach the pass",
    "systemverilog": "refused unless `default`: slang and UHDM are not the reader `yosys <file>.sv` uses",
    "read_systemverilog_flags": "refused when nonempty: no flag of the reference, and only a plugin "
    "front end reads them",
    "use_slang_plugin": "no conflict, unreachable under the mode: it only gates `plugin -i slang`, "
    "which the template renders when `systemverilog == slang`, and that is refused",
    "plugins": "`plugin -i` the user asked for; reads no file and renames no cell",
    # template variables `run()` passes, which carry settings the template does not name itself
    "defines": "design input: a bare `yosys` needs the same `-D` to read the same source",
    "ghdl_args": "VHDL has no bare `yosys <file>` reader; the plugin is what reads it at all",
    "liberty": "not a `yosys_fpga` setting (the template's `is defined` guard)",
    "debug": "`echo on` only",
    "verbose": "`echo on` only",
}


def test_every_setting_the_reader_script_renders_has_a_decision_for_the_mode():
    """The class `read_verilog_flags` was an instance of: a reader setting that reaches the
    mode's `read_verilog`/`read_slang` and so makes it read differently from `yosys <file>`."""
    import re

    from xeda.flows.yosys import yosys_fpga

    folder = Path(yosys_fpga.__file__).parent / "templates"
    used = {
        name
        for suffix in ("ys", "tcl")
        for name in re.findall(
            r"settings\.([a-z_0-9]+)|\b(defines|ghdl_args)\b",
            (folder / f"read_files.{suffix}").read_text(),
        )
        for name in name
        if name
    }
    assert used == set(
        READER_SETTINGS
    ), f"decide the mode's behavior for {sorted(used ^ set(READER_SETTINGS))}"
    for name, decision in READER_SETTINGS.items():
        if decision.startswith("refused"):
            assert name in CONFLICTS, name
        if name == "liberty":
            assert name not in YosysFpga.Settings.model_fields


def test_the_refusal_arrives_at_planning_before_any_tool_runs(tmp_path, toolchain):
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    with pytest.raises(FlowSettingsError) as raised:
        runner.plan(
            YosysFpga,
            _design(tmp_path),
            flow_settings={
                "fpga": PARTS["xilinx"],
                "clock": {"period": 5.0},
                "synth_pass_only": True,
                **PASS_ONLY_READER,
                "pre_synth_opt": True,
            },
        )
    assert "`synth_pass_only`" in str(raised.value)
    assert ": pre_synth_opt " in str(raised.value)
    assert ": read_verilog_flags " not in str(raised.value)
    assert not list((tmp_path / "run").glob("**/*.ys"))


def test_a_design_whose_own_attributes_reach_setattr_is_refused_too(tmp_path, toolchain):
    """`init()` merges `design.rtl.attributes` into `set_attribute`, which the class-level
    planning check cannot see: with no `hierarchy` before the pass, `setattr` would match no
    module and yosys would drop the attributes with a warning."""
    design = _design(tmp_path)
    design.rtl.attributes = {"keep": {"top": "true"}}
    settings = {
        "fpga": PARTS["xilinx"],
        "clock": {"period": 5.0},
        "synth_pass_only": True,
        **PASS_ONLY_READER,
    }
    YosysFpga.check_settings_supported(
        YosysFpga.Settings(**settings)  # type: ignore[arg-type]
    )  # the settings alone carry no attribute
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    with pytest.raises(FlowSettingsError) as raised:
        runner.run(YosysFpga, design, flow_settings=settings)
    assert "`synth_pass_only`" in str(raised.value)
    assert ": set_attribute " in str(raised.value)


def test_a_nested_producer_section_reaches_the_mode_and_its_refusals(tmp_path, toolchain):
    """`-s flows.yosys_fpga.synth_pass_only=true` on a consumer's run is the way the fork's
    project file turns the mode on, so the refusal has to arrive from there too."""
    from xeda.flows import Nextpnr

    runner = DefaultRunner(tmp_path / "run", display_results=False)
    with pytest.raises(FlowSettingsError) as raised:
        runner.plan(
            Nextpnr,
            _design(tmp_path),
            flow_settings=[
                f"fpga.part={PARTS['ecp5']}",
                "clock.period=5.0",
                "flows.yosys_fpga.synth_pass_only=true",
                "flows.yosys_fpga.read_verilog_flags=",
                "flows.yosys_fpga.prep=-flatten",
            ],
        )
    assert "`synth_pass_only`" in str(raised.value)
    assert ": prep " in str(raised.value)


#: the file-valued settings whose files the mode reads on purpose, each beside the reason: it is
#: an input of the design, read after the sources as `read_verilog -lib <file>` would be
REVIEWED_READS = {"verilog_lib"}

#: file-valued settings that are no read of this script: `lib_paths` are the directories GHDL
#: searches for its libraries, and only the ghdl front end that reads the design's VHDL uses them
NOT_A_READ = {"lib_paths"}


def _read_settings() -> list[str]:
    """Every setting of `yosys_fpga` that names a file it may read: a `Path` (or a list of them)
    that is neither a working location nor a delivered output (`dataclass.written_role`)."""
    return sorted(
        name
        for name, field in YosysFpga.Settings.model_fields.items()
        if "Path" in str(field.annotation)
        and written_role(YosysFpga.Settings, name) is None
        and name not in NOT_A_READ
    )


@pytest.mark.parametrize("setting", _read_settings())
def test_no_setting_makes_the_mode_read_a_file_the_reference_invocation_does_not(
    tmp_path, toolchain, setting
):
    """The class `clockgate_map` was an instance of: a setting that adds a `read_verilog`.

    Every `read_verilog` advances yosys's `autoidx`, so a file the mode reads beyond the sources
    is a netlist `yosys -p 'synth_<target> ...' <sources>` does not write. Each setting that
    names a file is therefore either refused under the mode, or one of the reviewed reads: a new
    one fails here until it is classified.
    """
    file = tmp_path / f"setting_{setting}.v"
    file.write_text("module " + file.stem + "(); endmodule\n")
    value: "str | list[str]" = (
        [str(file)]
        if "List" in str(YosysFpga.Settings.model_fields[setting].annotation)
        else str(file)
    )
    try:
        run_path = _launch(tmp_path, PARTS["xilinx"], synth_pass_only=True, **{setting: value})
    except FlowSettingsError as refused:
        assert f": {setting} " in str(refused)
        return
    script = (run_path / "yosys_fpga_synth.ys").read_text()
    assert (file.name in script) == (setting in REVIEWED_READS), script


# ------------------------------------------------------------- what the mode deliberately keeps


@BY_FORMAT
def test_the_mode_keeps_the_commands_that_cannot_move_a_cell(tmp_path, toolchain, script_format):
    """`check`, `stat` and the netlist writers stay: they observe or write, never transform.

    `check_assert` can still fail a run that `yosys -p synth_<target>` alone would not; that is
    the safer default and it changes no output.
    """
    run_path = _launch(tmp_path, PARTS["xilinx"], synth_pass_only=True, script_format=script_format)
    text = (run_path / f"yosys_fpga_synth.{script_format}").read_text()
    assert "check  -assert" in text.replace("\t", " ")
    assert _commands(run_path, script_format).count("tee") == 2  # `check` and `stat`
    assert "write_json" in _commands(run_path, script_format)
    results = json.loads((run_path / "results.json").read_text())
    assert results["success"] is True


@BY_FORMAT
def test_a_designs_parameters_still_reach_yosys_under_the_mode(tmp_path, toolchain, script_format):
    """`chparam` is how a design's parameters reach yosys, and it works on a deferred module.

    It is a read-time command, not a pass, so the mode keeps it: a wrong netlist would be a
    worse failure than one extra command.
    """
    design = _design(tmp_path)
    design.rtl.parameters = {"W": 8}
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    flow = runner.run(
        YosysFpga,
        design,
        flow_settings={
            "fpga": PARTS["xilinx"],
            "clock": {"period": 5.0},
            "synth_pass_only": True,
            **PASS_ONLY_READER,
            "script_format": script_format,
        },
    )
    assert flow is not None
    text = (Path(flow.run_path) / f"yosys_fpga_synth.{script_format}").read_text()
    assert "chparam -set W 8 top" in text


def test_a_systemverilog_design_under_the_mode_is_refused_before_any_tool_runs(tmp_path, toolchain):
    """`yosys <file>.sv` reads with the built-in reader, so the default `slang` front end would
    be a mismatch found only by comparing numbers: refused at planning, whatever the sources,
    and `use_slang_plugin=false` (which only stops the plugin loading) is no way round it."""
    root = tmp_path / "design"
    root.mkdir(exist_ok=True)
    (root / "top.sv").write_text(
        "module top(input logic clk, output logic q); assign q = clk; endmodule\n"
    )
    design = Design(
        name="top",
        design_root=root,
        rtl={"sources": ["top.sv"], "top": "top", "clock": {"port": "clk"}},
    )
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    base = {
        "fpga": PARTS["xilinx"],
        "clock": {"period": 5.0},
        "synth_pass_only": True,
        "read_verilog_flags": [],
    }
    for extra in ({}, {"use_slang_plugin": False}):
        with pytest.raises(FlowSettingsError) as raised:
            runner.plan(YosysFpga, design, flow_settings={**base, **extra})
        assert ": systemverilog " in str(raised.value)
    runner.plan(YosysFpga, design, flow_settings={**base, "systemverilog": "default"})
    assert not list((tmp_path / "run").glob("**/*.ys"))


def test_the_deviations_the_plan_enumerates_are_the_ones_the_default_script_renders(
    tmp_path, toolchain
):
    """Every xeda-only command in the default script is a known deviation, and none survives
    the mode: a new pre- or post-pass added later fails here until it is classified."""
    default = set(_commands(_launch(tmp_path, PARTS["xilinx"]), "ys"))
    mode = set(_commands(_launch(tmp_path, PARTS["xilinx"], synth_pass_only=True), "ys"))
    # `flatten`: xeda's recipe flattens for Xilinx by default, the pass alone does not -- and
    # Vivado reads a flat EDIF netlist only, which only a flattened Xilinx synthesis writes
    assert default - mode == {
        "hierarchy",
        "check",
        "proc",
        "flatten",
        "opt_clean",
        "scratchpad",
        "write_edif",
    }
    assert mode - default == set()
    assert not mode & DEVIATIONS


@pytest.mark.parametrize("mapping", [{"abc9": False}, {"noabc": True}])
def test_pass_only_accepts_flow3_when_abc9_mapping_is_disabled(mapping):
    settings = YosysFpga.Settings(
        fpga=PARTS["ice40"], synth_pass_only=True, flow3=True, **PASS_ONLY_READER, **mapping
    )
    YosysFpga.check_settings_supported(settings)
    assert settings.abc9_scratchpad() == []


ABC9_SCRIPTS = ("default", "default.area", "default.fast", "flow", "flow2", "flow3", "flow3mfs")


@pytest.mark.parametrize("script", ABC9_SCRIPTS)
@pytest.mark.parametrize("mode", [False, True])
@BY_FORMAT
def test_an_explicit_abc9_script_is_honored_in_either_recipe(
    tmp_path, toolchain, script, mode, script_format
):
    path = _launch(
        tmp_path,
        PARTS["xilinx"],
        synth_pass_only=mode,
        abc9_script=script,
        script_format=script_format,
    )
    text = (path / f"yosys_fpga_synth.{script_format}").read_text()
    assert f"scratchpad -copy abc9.script.{script} abc9.script" in text
    assert ("scratchpad -set abc9.D" in text) is (not mode)


def test_abc9_script_and_legacy_flow3_are_not_two_competing_choices():
    with pytest.raises(Exception, match="abc9_script.*flow3|flow3.*abc9_script"):
        YosysFpga.Settings(fpga=PARTS["xilinx"], abc9_script="flow2", flow3=False)
