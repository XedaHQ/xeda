"""`synth_pass_only`: the script is the synthesis pass's own recipe, and nothing else.

xeda's default `yosys_fpga` recipe elaborates and optimizes around `synth_<target>` and tells
ABC9 the clock period. That is deliberate -- it usually gives smaller and faster logic -- but it
is *not* what `yosys -p 'synth_<target> ...' <sources>` produces, and ABC9's mapping depends on
which it was. `synth_pass_only` is how a user reproduces the tool's own result, to compare
against it or to tell a xeda problem from a yosys one.

These launches run the process fakes of `tests/fake_tools` (`use_fake_fpga_tools`), so the
oracle runs in CI with no yosys installed. `tests/test_yosys_recipe_real.py` is the other half:
it checks that the netlist really is the pass's own, on the installed yosys.

Going through a whole launch is the point. The decision not to read the target's primitive
libraries early lives in `YosysFpga.run()`, not in the templates, so a render-level helper that
passes `primitive_libraries` itself would never see it.
"""

import json
from pathlib import Path

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
    # the primitives -- `synth_gowin` is the one target xeda has no libraries for
    reads = commands.count("read_verilog")
    assert reads == (1 if target == "gowin" else 1 + len(_libraries(PARTS[target])))


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
    libraries = [library for library in _libraries(PARTS[target])]
    if not libraries:
        pytest.skip("synth_gowin reads no library, so no entry can name one")
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
    `+/` entry is yosys' own spelling, and Gowin's pass reads no library to compare with."""

    def asked(*_):
        raise AssertionError("yosys' data directory was asked for")

    monkeypatch.setattr("xeda.flows.yosys.yosys_fpga.yosys_data_dir", asked)
    own = tmp_path / "own.v"
    own.write_text("module own(); endmodule\n")
    _launch(tmp_path, PARTS["xilinx"], synth_pass_only=True, verilog_lib=["+/xilinx/cells_sim.v"])
    _launch(tmp_path, PARTS["gowin"], synth_pass_only=True, verilog_lib=[str(own)])


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
                "verilog_lib": [str(library)],
            },
        )


def _libraries(part: "str | dict[str, str]") -> list:
    settings = YosysFpga.Settings(fpga=part, clock={"period": 5.0})  # type: ignore[arg-type]
    return settings.primitive_libraries((0, 63))


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
    assert settings.primitive_libraries((0, 63)) == _libraries(part)
    script = (_launch(tmp_path, part, synth_pass_only=True) / "yosys_fpga_synth.ys").read_text()
    reads = [line for line in script.splitlines() if line.startswith("read_verilog")]
    assert not [line for line in reads if " +/" in line], reads


@BY_FORMAT
def test_synth_pass_only_leaves_abc9_the_script_and_the_delay_the_pass_gives_it(
    tmp_path, toolchain, script_format
):
    """No `scratchpad`: not `flow3`, and not the clock period as ABC9's target delay.

    A constrained clock is a timing constraint, not a request for a scratchpad tweak, so it is
    not an error -- it is simply not passed on, which is what the tool's own flow does. This is
    where the default recipe earns most of its area and timing advantage.
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
    "flow3": True,
    "pre_synth_opt": True,
    "post_synth_opt": True,
    "splitnets": True,
    "post_synth_rename": ["-hide"],
    "black_box": ["leaf"],
    "keep_hierarchy": ["leaf"],
    "set_attribute": {"keep": {"top": "true"}},
    "set_mod_attribute": {"keep_hierarchy": {"leaf": 1}},
    "clockgate_map": "clock_gates.v",
    "stop_after": "rtl",
    "rtl_json": "rtl.json",
    "rtl_verilog": "rtl.v",
    "rtl_graph": "rtl.dot",
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
        **{setting: CONFLICTS[setting]},
    )
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
    conflicts = settings.synth_pass_only_conflicts()
    assert sorted(key for key, _ in conflicts) == sorted(asked)
    settings.synth_pass_only = False
    assert settings.synth_pass_only_conflicts() == []
    YosysFpga.check_settings_supported(settings)  # nothing conflicts without the mode


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
                "pre_synth_opt": True,
            },
        )
    assert "`synth_pass_only`" in str(raised.value)
    assert not list((tmp_path / "run").glob("**/*.ys"))


def test_a_design_whose_own_attributes_reach_setattr_is_refused_too(tmp_path, toolchain):
    """`init()` merges `design.rtl.attributes` into `set_attribute`, which the class-level
    planning check cannot see: with no `hierarchy` before the pass, `setattr` would match no
    module and yosys would drop the attributes with a warning."""
    design = _design(tmp_path)
    design.rtl.attributes = {"keep": {"top": "true"}}
    settings = {"fpga": PARTS["xilinx"], "clock": {"period": 5.0}, "synth_pass_only": True}
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
                "flows.yosys_fpga.flow3=true",
            ],
        )
    assert "`synth_pass_only`" in str(raised.value)
    assert ": flow3 " in str(raised.value)


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
            "script_format": script_format,
        },
    )
    assert flow is not None
    text = (Path(flow.run_path) / f"yosys_fpga_synth.{script_format}").read_text()
    assert "chparam -set W 8 top" in text


def test_systemverilog_read_by_a_plugin_under_the_mode_says_so(tmp_path, toolchain, caplog):
    """Which front end reads the sources is the user's choice, not the recipe's -- but
    `yosys <file>.sv` reads with the built-in reader, so the default `slang` would otherwise be
    a mismatch found only by comparing numbers."""
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
    base = {"fpga": PARTS["xilinx"], "clock": {"period": 5.0}, "synth_pass_only": True}
    with caplog.at_level("WARNING", logger="xeda.flows.yosys.yosys_fpga"):
        runner.run(YosysFpga, design, flow_settings={**base, "use_slang_plugin": False})
    assert "systemverilog=default" in caplog.text
    caplog.clear()
    with caplog.at_level("WARNING", logger="xeda.flows.yosys.yosys_fpga"):
        runner.run(
            YosysFpga,
            design,
            flow_settings={**base, "systemverilog": "default", "netlist_json": "built_in.json"},
        )
    assert "systemverilog=default" not in caplog.text


def test_the_deviations_the_plan_enumerates_are_the_ones_the_default_script_renders(
    tmp_path, toolchain
):
    """Every xeda-only command in the default script is a known deviation, and none survives
    the mode: a new pre- or post-pass added later fails here until it is classified."""
    default = set(_commands(_launch(tmp_path, PARTS["xilinx"]), "ys"))
    mode = set(_commands(_launch(tmp_path, PARTS["xilinx"], synth_pass_only=True), "ys"))
    assert default - mode == {"hierarchy", "check", "proc", "opt_clean", "scratchpad"}
    assert mode - default == set()
    assert not mode & DEVIATIONS
