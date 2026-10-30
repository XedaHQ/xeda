"""The `sby` flow: one SBY task per run, checked by its fresh status, exit and obligations.

The fake `sby` of these tests is a script in `tmp_path`. It copies the `[files]` of the generated
task as SBY does, writes the status, model and evidence files that the test asks for, and exits
with the code that SBY gives for that status. The real-tool tests at the end run SBY with Bitwuzla
through slang (SystemVerilog) and through the GHDL plugin (VHDL).
"""

import functools
import json
import os
import re
import shutil
import stat
import sys
import tempfile
import zipfile
from pathlib import Path
from typing import Any

import psutil
import pytest

from xeda import Design
from xeda.flow import FlowException, FlowSettingsException
from xeda.flow_runner import DefaultRunner
from xeda.flows.sby import Sby
from xeda.utils import ExecutableNotFound, settings_to_dict

from . import tool_utils

FAKE_SBY = r"""
import json, os, shutil, sys
from pathlib import Path

args = sys.argv[1:]
if args == ["--version"]:
    print("SBY v0.70-9-g79fc9ed")
    sys.exit(0)
with open("fake_sby_calls.jsonl", "a") as calls:
    calls.write(json.dumps({
        "argv": args,
        "cwd": os.getcwd(),
        "TMPDIR": os.environ.get("TMPDIR"),
        "XDG_STATE_HOME": os.environ.get("XDG_STATE_HOME"),
        "tmp_exists": os.path.isdir(os.environ.get("TMPDIR", "")),
        "PATH": os.environ["PATH"],
        "found": {name: shutil.which(name) for name in ("bitwuzla", "btorsim")},
    }) + "\n")
work = Path(args[args.index("-d") + 1])
config = Path(args[-1])
if work.exists() and any(p.is_file() for p in work.rglob("*")):
    print(f"ERROR: Directory '{work}' already exists, use -f to overwrite the existing directory.")
    sys.exit(1)
work.mkdir(parents=True, exist_ok=True)
section = None
for line in config.read_text().splitlines():
    if line.startswith("["):
        section = line.strip("[]")
    elif section == "files" and line.strip():
        dst, src = line.split()
        target = work / "src" / dst
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, target)
spec = json.loads(os.environ.get("FAKE_SBY", "{}"))
codes = {"PASS": 0, "FAIL": 2, "UNKNOWN": 4, "TIMEOUT": 8, "ERROR": 16, "CANCELLED": 32}
status = spec.get("status", "PASS")
code = spec.get("exit", codes.get(status, 1))
(work / "logfile.txt").write_text(spec.get("log", f"SBY [work] DONE ({status}, rc={code})\n"))
if spec.get("model", True):
    (work / "model").mkdir()
    sorts = {}
    for index, cell in enumerate(spec.get("cells", ["$check:assert"])):
        kind, _, flavor = cell.partition(":")
        parameters = {"FLAVOR": flavor} if flavor else {}
        sorts.setdefault(kind, []).append({"name": f"c{index}", "parameters": parameters})
    top = {
        "name": "top",
        "attributes": {"top": "00000000000000000000000000000001"},
        "cell_sorts": [{"type": t, "cells": c} for t, c in sorts.items()],
    }
    modules = spec.get("modules", [top])
    (work / "model" / "design.json").write_text(json.dumps({"modules": modules}))
    (work / "model" / "design.log").write_text("yosys\n")
for index in range(spec.get("traces", 0)):
    (work / "engine_0").mkdir(exist_ok=True)
    (work / "engine_0" / f"trace{index}.vcd").write_text(f"trace {index}\n")
if "link" in spec:
    os.symlink(spec["link"], work / "outside")
if "status_text" in spec:
    (work / "status").write_text(spec["status_text"])
elif not spec.get("no_status"):
    (work / "status").write_text(f"{status} {codes[status]} 0\n")
    (work / status).write_text("summary\n")
sys.exit(code)
"""

SV_TOP = "module top(input clk, input a); always @(posedge clk) assert(a); endmodule\n"


def _executable(path: Path, text: str) -> None:
    path.write_text(text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


#: The version banner of the stub of each program that SBY may start.
STUBS = {
    "bitwuzla": "0.9.1",
    "btorsim": "btorsim 3.2.4",
    "rIC3": "rIC3 1.5.2",
    "yosys": "Yosys 0.70+255 (git sha1 72b0054f0, Release)",
    "yosys-smtbmc": "yosys-smtbmc",
    "yosys-witness": "yosys-witness",
}
#: The variables in which SBY looks for a program before `PATH`.
SBY_VARIABLES = {
    "YOSYS": "yosys",
    "SMTBMC": "yosys-smtbmc",
    "WITNESS": "yosys-witness",
    "RIC3": "rIC3",
}


def _stubs(directory: Path, version: str | None = None) -> None:
    directory.mkdir(exist_ok=True)
    for name, banner in STUBS.items():
        _executable(directory / name, f"#!/bin/sh\necho '{version or banner}'\n")


@pytest.fixture
def fake_sby(tmp_path, monkeypatch):
    """Put the fake `sby` and a stub of every program it may start in one directory, first on a
    minimal PATH. Return that directory."""
    bin_dir = tmp_path / "fake_bin"
    _stubs(bin_dir)
    script = bin_dir / "fake_sby.py"
    script.write_text(FAKE_SBY)
    _executable(bin_dir / "sby", f"#!/bin/sh\nexec '{sys.executable}' '{script}' \"$@\"\n")
    monkeypatch.setenv("PATH", os.pathsep.join([str(bin_dir), "/usr/bin", "/bin"]))
    monkeypatch.delenv("FAKE_SBY", raising=False)
    for variable in SBY_VARIABLES:
        monkeypatch.delenv(variable, raising=False)
    return bin_dir


def _spec(monkeypatch, **spec: Any) -> None:
    monkeypatch.setenv("FAKE_SBY", json.dumps(spec))


def _design(root: Path, sources: dict[str, str] | None = None, **rtl: Any) -> Design:
    """A design in `root` with the given files (relative path -> text); the first lists first."""
    sources = sources if sources is not None else {"top.sv": SV_TOP}
    for name, text in sources.items():
        (root / name).parent.mkdir(parents=True, exist_ok=True)
        (root / name).write_text(text)
    rtl = {"sources": list(sources), "top": "top", **rtl}
    language = rtl.pop("language", {})
    return Design(name="d", root=root, rtl=rtl, language=language)


def _run(tmp_path: Path, design: Design, **settings: Any):
    settings.setdefault("mode", "bmc")
    if settings["mode"] == "prove":
        settings.setdefault("timeout", "1 min")  # a proof has no bound of its own: it needs one
    flow = DefaultRunner(tmp_path / "xeda_run", display_results=False).run_flow(
        Sby, design, settings
    )
    assert flow is not None
    return flow


def _document(flow) -> dict:
    return json.loads((flow.run_path / "results.json").read_text())


def _calls(flow) -> list[dict]:
    lines = (flow.run_path / "fake_sby_calls.jsonl").read_text().splitlines()
    return [json.loads(line) for line in lines]


def _archived(flow) -> set[str]:
    with zipfile.ZipFile(flow.run_path / flow.artifacts["evidence_archive"]) as archive:
        return set(archive.namelist())


# ---- planning ----------------------------------------------------------------------------


def test_sby_needs_an_explicit_mode(tmp_path, fake_sby):
    with pytest.raises(FlowSettingsException, match="`mode`"):
        DefaultRunner(tmp_path / "xeda_run", display_results=False).run_flow(
            Sby, _design(tmp_path), {}
        )


def test_sby_needs_an_explicit_top(tmp_path, fake_sby):
    design = _design(tmp_path, top=None)
    with pytest.raises(FlowSettingsException, match=r"rtl\.top"):
        _run(tmp_path, design)


@pytest.mark.parametrize(
    "sources, rtl, match",
    [
        (
            {"top.sv": SV_TOP, "dut.vhd": "--\n"},
            {},
            r"sby cannot read the design's Vhdl source\(s\) \S*dut\.vhd together with its "
            r"SystemVerilog source\(s\) \S*top\.sv: ",
        ),
        ({"rtl dir/top.sv": SV_TOP}, {}, "rtl dir/top.sv"),
        ({"top$x.sv": SV_TOP}, {}, r"top\$x.sv"),
        ({"top.sv": SV_TOP}, {"defines": {"SYNTHESIS": None}}, "SYNTHESIS"),
        ({"top.sv": SV_TOP}, {"defines": {"FORMAL": 1}}, "FORMAL"),
        ({"top.sv": SV_TOP}, {"defines": {"W": "a b"}}, "W"),
        ({"top.sv": SV_TOP}, {"parameters": {"NAME": 'say "hi"'}}, "NAME"),
        ({"top.sv": SV_TOP}, {"top": "top(rtl)"}, "rtl.top"),
        ({"top.vhd": "--\n"}, {"language": {"vhdl": {"synopsys": True}}}, "synopsys"),
        ({"inc/a.vh": "", "INC/A.vh": "", "top.sv": SV_TOP}, {}, "INC/A.vh"),
    ],
    ids=[
        "mixed languages",
        "whitespace",
        "unsafe character",
        "SYNTHESIS",
        "FORMAL",
        "define with whitespace",
        "parameter with quote",
        "top form",
        "synopsys",
        "case collision",
    ],
)
def test_sby_refuses_what_it_cannot_stage_or_read_exactly(tmp_path, fake_sby, sources, rtl, match):
    # Two names that differ only in case are one file on a case-insensitive file system and two
    # on another; staging refuses them on both.
    root = tmp_path / "design"
    root.mkdir()
    design = _design(root, sources, **rtl)
    with pytest.raises(FlowSettingsException, match=match):
        _run(tmp_path, design)
    assert not (tmp_path / "xeda_run" / "d" / "sby" / "fake_sby_calls.jsonl").exists()


def test_sby_refuses_a_source_outside_the_design_root(tmp_path, fake_sby):
    (tmp_path / "shared.sv").write_text(SV_TOP)
    root = tmp_path / "design"
    root.mkdir()
    design = Design(name="d", root=root, rtl={"sources": ["../shared.sv"], "top": "top"})
    with pytest.raises(FlowSettingsException, match="outside the design root"):
        _run(tmp_path, design)


def test_sby_refuses_library_paths(tmp_path, fake_sby):
    with pytest.raises(FlowSettingsException, match="lib_paths"):
        _run(tmp_path, _design(tmp_path), lib_paths=[["lib", str(tmp_path)]])


@pytest.mark.parametrize("timeout", ["1500 ms", "0.5 s"])
def test_the_task_timeout_is_a_whole_positive_number_of_seconds(tmp_path, fake_sby, timeout):
    """SBY takes whole seconds; Xeda refuses another limit at planning rather than round it."""
    with pytest.raises(FlowSettingsException, match="whole number of seconds"):
        _run(tmp_path, _design(tmp_path), timeout=timeout)


# ---- the generated task --------------------------------------------------------------------


def _config(flow) -> str:
    return (flow.run_path / flow.artifacts["config"]).read_text()


def test_the_systemverilog_task_keeps_every_property_and_reads_strictly(tmp_path, fake_sby):
    sources = {"inc/defs.svh": "", "rtl/sub.sv": "", "top.sv": SV_TOP}
    design = _design(tmp_path, sources, parameters={"OK": 1, "NAME": "x"}, defines={"W": 4})
    flow = _run(tmp_path, design, mode="bmc", depth=7, timeout="2 min")
    config = _config(flow)

    assert "mode bmc\n" in config and "depth 7\n" in config and "timeout 120\n" in config
    assert "[engines]\nsmtbmc bitwuzla\n" in config
    assert "expect" not in config and "nopresat" not in config
    read = next(line for line in config.splitlines() if line.startswith("read_slang"))
    words = read.split()
    assert words[:3] == ["read_slang", "--no-synthesis-define", "-DFORMAL"]
    assert "-DW=4" in words and "-Iinc" in words and "--top" in words
    assert words[words.index("--top") + 1] == "top"
    assert words.index("rtl/sub.sv") < words.index("top.sv")  # design order
    assert "inc/defs.svh" not in words  # a header is found through `-I`, not compiled
    assert "-G" in words and "OK=1" in words and 'NAME="x"' in words
    assert "--best-effort-hierarchy" not in read and "--ignore-assertions" not in read
    lines = config.splitlines()
    guards = [i for i, line in enumerate(lines) if line.startswith("select -assert")]
    hierarchy = lines.index("hierarchy -check -top top")
    prep = lines.index("prep -top top")
    assert guards and hierarchy < min(guards) and max(guards) < prep
    assert any("a:anyconst" in lines[i] and "-assert-none" in lines[i] for i in guards)
    assert any("r:FLAVOR=assert" in lines[i] and "-assert-any" in lines[i] for i in guards)
    assert "chformal" not in config and "setundef" not in config


def test_the_cover_task_asks_for_a_cover_goal(tmp_path, fake_sby, monkeypatch):
    _spec(monkeypatch, cells=["$check:cover"])
    flow = _run(tmp_path, _design(tmp_path), mode="cover")
    assert flow.outcome == "passed"
    config = _config(flow)
    assert "r:FLAVOR=cover" in config and "r:FLAVOR=assert" not in config


def test_the_vhdl_task_elaborates_with_formal_and_psl(tmp_path, fake_sby):
    design = _design(
        tmp_path,
        {"pkg.vhd": "--\n", "top.vhd": "--\n"},
        parameters={"OK": True, "N": 3},
        language={"vhdl": {"standard": "2008"}},
    )
    flow = _run(tmp_path, design)
    config = _config(flow)
    ghdl = next(line for line in config.splitlines() if line.startswith("ghdl "))
    words = ghdl.split()
    assert "--std=08" in words and "-fpsl" in words
    assert "-gOK=true" in words and "-gN=3" in words
    assert words[-2:] == ["-e", "top"]
    assert words.index("pkg.vhd") < words.index("top.vhd")
    assert "plugin -i ghdl" in config and "read_slang" not in config
    for dropped in ("--no-formal", "--assert-assumes", "--assume-asserts", "--no-assert-cover"):
        assert dropped not in config


# ---- the engine of each mode ---------------------------------------------------------------


@pytest.mark.parametrize("mode", ["bmc", "cover"])
def test_bmc_and_cover_search_with_smtbmc_and_bitwuzla_to_a_default_depth(
    tmp_path, fake_sby, monkeypatch, mode
):
    _spec(monkeypatch, cells=["$check:cover" if mode == "cover" else "$check:assert"])
    flow = _run(tmp_path, _design(tmp_path), mode=mode)
    config = _config(flow)
    assert "[engines]\nsmtbmc bitwuzla\n" in config and "\ndepth 20\n" in config
    assert flow.results["formal.depth"] == 20
    assert (flow.results["formal.engine"], flow.results["formal.solver"]) == ("smtbmc", "bitwuzla")


def test_prove_runs_the_ic3_engine_of_ric3_and_names_no_depth(tmp_path, fake_sby):
    """`btor rIC3 -e ic3` is one process: its default portfolio writes `/tmp/rIC3`. SBY gives it
    no depth, so the task has none and the run claims no induction bound."""
    flow = _run(tmp_path, _design(tmp_path), mode="prove")
    config = _config(flow)
    assert "[engines]\nbtor rIC3 -e ic3\n" in config and "smtbmc" not in config
    assert "\ndepth " not in config and "\ntimeout 60\n" in config
    assert "formal.depth" not in flow.results
    assert (flow.results["formal.engine"], flow.results["formal.solver"]) == ("btor", "rIC3")


def test_prove_refuses_a_depth_that_nothing_would_check(tmp_path, fake_sby):
    with pytest.raises(FlowSettingsException, match=r"`prove` has no `depth`"):
        _run(tmp_path, _design(tmp_path), mode="prove", depth=20)


def test_prove_needs_a_time_limit_because_the_proof_has_no_bound(tmp_path, fake_sby):
    with pytest.raises(FlowSettingsException, match="`timeout`"):
        DefaultRunner(tmp_path / "xeda_run", display_results=False).run_flow(
            Sby, _design(tmp_path), {"mode": "prove"}
        )


#: The programs that SBY starts in each mode, by the option of SBY that names it (None: SBY finds
#: it on its PATH).
PROGRAMS = {
    "bmc": {"yosys": "--yosys", "yosys-smtbmc": "--smtbmc", "bitwuzla": None},
    "prove": {"yosys": "--yosys", "rIC3": "--rIC3", "yosys-witness": "--witness", "btorsim": None},
}
PROGRAMS["cover"] = PROGRAMS["bmc"]


@pytest.mark.parametrize(
    "mode, unused",
    [
        ("bmc", ["rIC3", "yosys-witness", "btorsim"]),
        ("cover", ["rIC3", "yosys-witness", "btorsim"]),
        ("prove", ["bitwuzla", "yosys-smtbmc"]),
    ],
)
def test_a_mode_needs_only_the_programs_of_its_engine(
    tmp_path, fake_sby, monkeypatch, mode, unused
):
    for name in unused:
        (fake_sby / name).unlink()
    _spec(monkeypatch, cells=["$check:cover" if mode == "cover" else "$check:assert"])
    flow = _run(tmp_path, _design(tmp_path), mode=mode)
    assert flow.outcome == "passed", _document(flow).get("error")
    assert not {Path(tool["executable"]).name for tool in flow.results.tools} & set(unused)
    assert set(PROGRAMS[mode]) <= set(Sby.executables)


@pytest.mark.parametrize("mode", ["bmc", "prove"])
def test_sby_runs_and_records_the_programs_beside_it_and_no_decoy(
    tmp_path, fake_sby, monkeypatch, mode
):
    """A decoy of each program comes first on PATH and in the variables that SBY reads, as when a
    wrapper of the OSS CAD Suite puts its own `bin` first. SBY gets the absolute path of each
    program beside `sby`, as given on PATH, and that directory first on its PATH, so a program
    without an option of SBY is found there too. The results record those programs."""
    decoys = tmp_path / "decoys"
    _stubs(decoys, version="9.9.9")
    monkeypatch.setenv("PATH", os.pathsep.join([str(decoys), os.environ["PATH"]]))
    for variable, name in SBY_VARIABLES.items():
        monkeypatch.setenv(variable, str(decoys / name))
    flow = _run(tmp_path, _design(tmp_path), mode=mode)
    (call,) = _calls(flow)
    options = call["argv"][:-3]
    assert dict(zip(options[::2], options[1::2])) == {
        option: str(fake_sby / name) for name, option in PROGRAMS[mode].items() if option
    }
    assert call["PATH"].split(os.pathsep)[0] == str(fake_sby)
    solver = next(name for name, option in PROGRAMS[mode].items() if option is None)
    assert call["found"][solver] == str(fake_sby / solver)
    versions = {tool["executable"]: tool["version"] for tool in flow.results.tools}
    engine = {"bmc": "bitwuzla", "prove": "rIC3"}[mode]
    assert set(versions) == {str(fake_sby / name) for name in ("sby", "yosys", engine)}
    assert versions[str(fake_sby / engine)] == {"bmc": "0.9.1", "prove": "1.5.2"}[mode]
    assert versions[str(fake_sby / "yosys")].startswith("0.70")
    assert "9.9.9" not in versions.values()


@pytest.mark.parametrize("inherit_env", [True, False])
def test_sby_selects_the_bundle_from_the_flow_path(tmp_path, fake_sby, inherit_env):
    selected = tmp_path / "selected_bin"
    shutil.copytree(fake_sby, selected)
    flow = _run(
        tmp_path,
        _design(tmp_path),
        env={"PATH": str(selected)},
        inherit_env=inherit_env,
    )
    assert flow.outcome == "passed", _document(flow).get("error")
    (call,) = _calls(flow)
    assert set(call["PATH"].split(os.pathsep)) == {str(selected)}
    assert {Path(tool["executable"]).parent for tool in flow.results.tools} == {selected}


@pytest.mark.parametrize(
    "mode, missing",
    [
        ("bmc", "yosys"),
        ("bmc", "yosys-smtbmc"),
        ("bmc", "bitwuzla"),
        ("cover", "bitwuzla"),
        ("prove", "rIC3"),
        ("prove", "yosys-witness"),
        ("prove", "btorsim"),
    ],
)
def test_a_program_missing_beside_sby_fails_before_sby_starts_and_nothing_replaces_it(
    tmp_path, fake_sby, monkeypatch, mode, missing
):
    """A program of the same name later on PATH is no fallback: an SBY wrapper would not run it."""
    others = tmp_path / "others"
    _stubs(others)
    monkeypatch.setenv("PATH", os.pathsep.join([os.environ["PATH"], str(others)]))
    (fake_sby / missing).unlink()
    with pytest.raises(ExecutableNotFound, match=f"'{missing}'.*{fake_sby}"):
        _run(tmp_path, _design(tmp_path), mode=mode)
    run_path = tmp_path / "xeda_run" / "d" / "sby"
    assert not (run_path / "fake_sby_calls.jsonl").exists()
    assert json.loads((run_path / "results.json").read_text())["error"]["code"] == (
        "executable_not_found"
    )


def test_sby_in_a_directory_that_a_shell_command_cannot_hold_is_refused(
    tmp_path, fake_sby, monkeypatch
):
    """SBY puts the path of each program into a command of `sh` without quotes."""
    spaced = tmp_path / "tool bin"
    shutil.copytree(fake_sby, spaced)
    monkeypatch.setenv("PATH", os.pathsep.join([str(spaced), "/usr/bin", "/bin"]))
    with pytest.raises(FlowException, match="tool bin.*letters, digits"):
        _run(tmp_path, _design(tmp_path))
    assert not list((tmp_path / "xeda_run").rglob("fake_sby_calls.jsonl"))


def test_a_run_that_stops_before_sby_starts_leaves_no_earlier_evidence(
    tmp_path, fake_sby, monkeypatch
):
    _spec(monkeypatch, status="FAIL", traces=1)
    first = _run(tmp_path, _design(tmp_path))
    assert first.outcome == "rejected" and (first.run_path / "sby_evidence.zip").is_file()
    (fake_sby / "bitwuzla").unlink()
    with pytest.raises(ExecutableNotFound):
        _run(tmp_path, _design(tmp_path))
    assert not (first.run_path / "sby").exists()
    assert not (first.run_path / "sby_evidence.zip").exists()
    document = _document(first)
    assert document["error"]["code"] == "executable_not_found"
    assert "evidence_archive" not in document.get("artifacts", {})


# ---- parameters ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text", ["0", "1", "-3", "1.5", "1e3", "0x10", "true", "False", "8'hff", "4'd5"]
)
def test_sby_refuses_a_parameter_that_reads_as_a_number_or_a_boolean_in_text(
    tmp_path, fake_sby, text
):
    """The task would pass text as a string, `-G OK="0"`, and `assert(OK)` would pass. The
    refusal names the typed YAML value; the design is never read as another theorem."""
    with pytest.raises(FlowSettingsException, match=r"`OK`.*typed YAML"):
        _run(tmp_path, _design(tmp_path, parameters={"OK": text}))
    assert not list((tmp_path / "xeda_run").rglob("fake_sby_calls.jsonl"))


def test_a_parameter_from_the_command_line_cannot_silently_change_the_theorem(tmp_path, fake_sby):
    """`--design-updates rtl.parameters.OK=0` is the text `0`, as in a quoted YAML string."""
    (tmp_path / "top.sv").write_text(SV_TOP)
    design = tmp_path / "design.yaml"
    design.write_text(json.dumps({"name": "d", "rtl": {"sources": ["top.sv"], "top": "top"}}))
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    updated = Design.load(design).with_updates(settings_to_dict(["rtl.parameters.OK=0"]))
    with pytest.raises(FlowSettingsException, match=r"`OK`.*typed YAML"):
        runner.run(Sby, updated, flow_settings=["sby.mode=bmc"])
    # The same value as a typed YAML number is the number, and a design file gives it so.
    design.write_text(
        json.dumps(
            {"name": "d", "rtl": {"sources": ["top.sv"], "top": "top", "parameters": {"OK": 0}}}
        )
    )
    flow = runner.run(Sby, design, flow_settings=["sby.mode=bmc"])
    assert flow is not None and "OK=0" in _config(flow).split()


def test_typed_numbers_booleans_sized_binary_and_plain_text_are_parameters(tmp_path, fake_sby):
    parameters = {"N": 3, "B": True, "S": "x", "BITS": "4'b0101", "NEG": -2}
    flow = _run(tmp_path, _design(tmp_path, parameters=parameters))
    words = next(
        line for line in _config(flow).splitlines() if line.startswith("read_slang")
    ).split()
    assert {"N=3", "B=1'b1", 'S="x"', "BITS=4'b0101", "NEG=-2"} <= set(words)


def test_a_second_root_checks_each_parameter_name_against_the_top(tmp_path, fake_sby):
    """slang ignores a `-G` name that the top does not have. A second root, compiled with the
    design, assigns each parameter to the top by name, so a name that the top lacks is an error.
    `hierarchy -top` then removes that root. A design without parameters has none."""
    flow = _run(tmp_path, _design(tmp_path, parameters={"N": 3, "S": "x"}))
    config = _config(flow)
    assert (
        "[file xeda_sby_parameters.sv]\n"
        'module xeda_sby_parameters; top #(.N(3), .S("x")) dut(); endmodule\n'
    ) in config
    words = next(line for line in config.splitlines() if line.startswith("read_slang")).split()
    assert words[words.index("--top") + 1] == "top"
    assert words[words.index("--top", words.index("--top") + 1) + 1] == "xeda_sby_parameters"
    assert "xeda_sby_parameters.sv" in words
    assert "hierarchy -check -top top" in config
    assert "xeda_sby_parameters" not in _config(_run(tmp_path, _design(tmp_path)))


VALIDATOR = "xeda_sby_parameters.sv"


def _validator_design(root: Path, top: str, name: str, kind: str | None, text: str, **rtl: Any):
    """A design with `top` and a second source `name`, of the stated source type `kind` if any."""
    root.mkdir()
    (root / top).write_text(SV_TOP if top.endswith(".sv") else "--\n")
    (root / name).parent.mkdir(exist_ok=True)
    (root / name).write_text(text)
    sources = [top, name if kind is None else {"file": name, "type": kind}]
    return Design(name="d", root=root, rtl={"sources": sources, "top": "top", **rtl})


@pytest.mark.parametrize(
    "name, kind",
    [
        (VALIDATOR, None),
        ("Xeda_Sby_Parameters.sv", None),
        (VALIDATOR, "SVHeader"),
        (VALIDATOR, "VerilogHeader"),
        (VALIDATOR, "MemoryFile"),
    ],
    ids=["unit", "other case", "SV header", "Verilog header", "memory file"],
)
def test_a_source_named_like_the_parameters_file_is_refused(tmp_path, fake_sby, name, kind):
    """The task writes `xeda_sby_parameters.sv` as the second root, and a staged file of that name
    replaces it. An empty module of that name and the parameter `NOPE`, which the top lacks, once
    made `bmc` pass. The rule holds for every staged type and letter case."""
    design = _validator_design(
        tmp_path / "design",
        "top.sv",
        name,
        kind,
        "module xeda_sby_parameters; endmodule\n",
        parameters={"NOPE": 0},
    )
    with pytest.raises(FlowSettingsException, match=r"xeda_sby_parameters\.sv"):
        _run(tmp_path, design)
    assert not list((tmp_path / "xeda_run").rglob("fake_sby_calls.jsonl"))


@pytest.mark.parametrize(
    "top, name, kind, parameters",
    [
        ("top.sv", VALIDATOR, None, {}),
        ("top.sv", f"sub/{VALIDATOR}", None, {"N": 3}),
        ("top.vhd", VALIDATOR, "MemoryFile", {"N": 3}),
    ],
    ids=["no parameters", "another path", "VHDL"],
)
def test_a_source_named_like_the_parameters_file_stays_when_the_task_has_no_such_file(
    tmp_path, fake_sby, top, name, kind, parameters
):
    """Only Verilog and SystemVerilog with parameters get the file, and only the same path is
    replaced. Here the task keeps the source, and the file exists exactly when it is needed."""
    design = _validator_design(
        tmp_path / "design", top, name, kind, "module other; endmodule\n", parameters=parameters
    )
    flow = _run(tmp_path, design)
    assert flow.outcome == "passed", _document(flow).get("error")
    assert (flow.run_path / "sby" / "src" / name).read_text() == "module other; endmodule\n"
    assert (f"[file {VALIDATOR}]" in _config(flow)) == (top == "top.sv" and bool(parameters))


def test_a_vhdl_generic_given_as_text_is_read_by_ghdl_as_its_type(tmp_path, fake_sby):
    """GHDL converts the text of `-gN=0` by the type of the generic, so text is not ambiguous."""
    design = _design(tmp_path, {"top.vhd": "--\n"}, parameters={"N": "0", "OK": "true"})
    flow = _run(tmp_path, design)
    assert {"-gN=0", "-gOK=true"} <= set(_config(flow).split())


# ---- staging -------------------------------------------------------------------------------


def test_staging_keeps_the_layout_of_the_design_root(tmp_path, fake_sby):
    """Same base names in two directories, a nested include and a memory file keep their place
    relative to the design root, so a relative `include` or `$readmemh` finds the same file. The
    design root and the run root have spaces; no staged name has one."""
    root = tmp_path / "my design"
    root.mkdir()
    sources = {
        "a/inc/defs.svh": "`define A 1\n",
        "b/inc/defs.svh": "`define B 1\n",
        "a/core.sv": "a\n",
        "b/core.sv": "b\n",
        "mem/init.hex": "00\n",
        "top.sv": SV_TOP,
    }
    design = _design(root, sources)
    flow = DefaultRunner(tmp_path / "run root", display_results=False).run_flow(
        Sby, design, {"mode": "bmc"}
    )
    assert flow is not None and flow.outcome == "passed", _document(flow).get("error")
    work_src = flow.run_path / "sby" / "src"
    for name, text in sources.items():
        assert (work_src / name).read_text() == text, name
    files = _config(flow).split("[files]\n", 1)[1].split()
    assert all(" " not in word for word in files)
    assert {"a/inc/defs.svh", "b/inc/defs.svh", "mem/init.hex"} <= set(files)


def test_a_staged_source_is_refreshed_from_the_original(tmp_path, fake_sby):
    flow = _run(tmp_path, _design(tmp_path))
    (tmp_path / "top.sv").write_text("module top; endmodule // edited\n")
    flow = _run(tmp_path, _design(tmp_path, {"top.sv": "module top; endmodule // edited\n"}))
    assert (flow.run_path / "sby" / "src" / "top.sv").read_text().endswith("// edited\n")


def test_sby_runs_in_the_run_directory_with_absolute_local_temporary_state(tmp_path, fake_sby):
    flow = _run(tmp_path, _design(tmp_path))
    (call,) = _calls(flow)
    assert Path(call["cwd"]).resolve() == flow.run_path.resolve()
    for variable in ("TMPDIR", "XDG_STATE_HOME"):
        value = Path(call[variable])
        assert value.is_absolute() and value.resolve().is_relative_to(flow.run_path.resolve())
    assert call["tmp_exists"]
    assert call["argv"][-3:-1] == ["-d", "sby"] and "-f" not in call["argv"]
    assert not Path(call["TMPDIR"]).exists()  # removed after the run


def test_sby_never_reuses_a_result(tmp_path, fake_sby):
    """SBY starts yosys, yosys-smtbmc and the solver itself, so Xeda cannot vouch for a result."""
    first = _run(tmp_path, _design(tmp_path))
    second = _run(tmp_path, _design(tmp_path))
    assert first.outcome == second.outcome == "passed"
    assert len(_calls(second)) == 2


# ---- verdicts ------------------------------------------------------------------------------


def test_a_fresh_pass_with_an_assertion_passes(tmp_path, fake_sby, monkeypatch):
    _spec(monkeypatch, cells=["$check:assert", "$check:assume", "$assert", "$check:cover"])
    flow = _run(tmp_path, _design(tmp_path), depth=5)
    assert flow.outcome == "passed"
    results = flow.results
    assert results["formal.status"] == "PASS"
    assert (results["formal.mode"], results["formal.depth"]) == ("bmc", 5)
    assert (results["formal.engine"], results["formal.solver"]) == ("smtbmc", "bitwuzla")
    assert (results["formal.assertions"], results["formal.assumptions"]) == (2, 1)
    assert results["formal.covers"] == 1
    assert {"log", "config", "status", "task_logfile", "evidence_archive"} <= set(flow.artifacts)
    assert {"model/design.json", "logfile.txt", "status"} <= _archived(flow)


def test_obligations_count_every_instance_of_a_module(tmp_path, fake_sby, monkeypatch):
    sub = {
        "name": "sub",
        "attributes": {},
        "cell_sorts": [
            {"type": "$check", "cells": [{"name": "c", "parameters": {"FLAVOR": "assert"}}]}
        ],
    }
    top = {
        "name": "top",
        "attributes": {"top": "1"},
        "cell_sorts": [{"type": "sub", "cells": [{"name": "u0"}, {"name": "u1"}]}],
    }
    _spec(monkeypatch, modules=[top, sub])
    flow = _run(tmp_path, _design(tmp_path))
    assert flow.outcome == "passed" and flow.results["formal.assertions"] == 2


@pytest.mark.parametrize(
    "mode, message",
    [("bmc", "within depth 9"), ("prove", "an assertion fails"), ("cover", "within depth 9")],
)
def test_a_fresh_fail_is_a_rejection_with_its_evidence(
    tmp_path, fake_sby, monkeypatch, mode, message
):
    cell = "$check:cover" if mode == "cover" else "$check:assert"
    _spec(monkeypatch, status="FAIL", cells=[cell], traces=3)
    settings = {} if mode == "prove" else {"depth": 9}  # a proof has no depth to name
    flow = _run(tmp_path, _design(tmp_path), mode=mode, **settings)
    assert flow.outcome == "rejected"
    assert message in flow.rejection
    assert ("depth" in flow.rejection) == (mode != "prove")
    assert flow.results["formal.status"] == "FAIL"
    assert {f"engine_0/trace{i}.vcd" for i in range(3)} <= _archived(flow)
    assert _document(flow)["error"]["code"] == "rejected"


@pytest.mark.parametrize("status", ["UNKNOWN", "TIMEOUT", "ERROR", "CANCELLED"])
def test_an_inconclusive_status_fails_with_its_evidence(tmp_path, fake_sby, monkeypatch, status):
    _spec(monkeypatch, status=status, log="SBY [sby] base: ERROR: something broke\n")
    flow = _run(tmp_path, _design(tmp_path))
    assert flow.outcome == "failed"
    assert flow.results["formal.status"] == status
    error = _document(flow)["error"]
    assert error["code"] == "nonzero_exit" and status in error["message"]
    assert "logfile.txt" in _archived(flow)


@pytest.mark.parametrize(
    "line, reason",
    [
        ("SBY [sby] base: top.sv:1: ERROR: unsupported SVA", "top.sv:1: ERROR: unsupported SVA"),
        ("SBY [sby] base: ERROR: Assertion failed: selection is empty: t:$assert", "has no assert"),
        ("SBY [sby] base: ERROR: Assertion failed: selection is not empty: a:anyconst", "anyconst"),
        ("SBY [sby] engine_0: ##   0:00:00  Assumptions are unsatisfiable!", "unsatisfiable"),
    ],
)
def test_an_sby_error_names_its_cause(tmp_path, fake_sby, monkeypatch, line, reason):
    _spec(monkeypatch, status="ERROR", log=f"SBY [sby] base: starting\n{line}\n")
    flow = _run(tmp_path, _design(tmp_path))
    assert reason in _document(flow)["error"]["message"]


@pytest.mark.parametrize(
    "spec",
    [
        {"status": "PASS", "exit": 2},
        {"status": "FAIL", "exit": 0},
        {"status_text": "PASS\n", "exit": 0},
        {"status_text": "PASS 1 0\n", "exit": 0},
        {"status_text": "MAYBE 0 0\n", "exit": 0},
        {"no_status": True, "exit": 0},
        {"no_status": True, "exit": 2},
        {"no_status": True, "exit": 1},
    ],
    ids=[
        "PASS with exit 2",
        "FAIL with exit 0",
        "truncated",
        "PASS with code 1",
        "unknown status",
        "no status, exit 0",
        "no status, exit 2",
        "no status, exit 1",
    ],
)
def test_a_status_that_does_not_agree_with_the_exit_fails(tmp_path, fake_sby, monkeypatch, spec):
    _spec(monkeypatch, **spec)
    flow = _run(tmp_path, _design(tmp_path))
    assert flow.outcome == "failed"
    assert flow.rejection is None


def test_a_status_of_an_earlier_run_is_never_read(tmp_path, fake_sby, monkeypatch):
    _spec(monkeypatch, traces=1)
    assert _run(tmp_path, _design(tmp_path)).outcome == "passed"
    _spec(monkeypatch, no_status=True, exit=0, model=False)
    flow = _run(tmp_path, _design(tmp_path))
    assert flow.outcome == "failed"
    assert not (flow.run_path / "sby" / "status").exists()
    assert "engine_0/trace0.vcd" not in _archived(flow)
    assert "status" not in flow.artifacts


@pytest.mark.parametrize(
    "mode, cells",
    [
        ("bmc", []),
        ("bmc", ["$check:assume"]),
        ("bmc", ["$check:cover"]),
        ("prove", ["$assume"]),
        ("cover", ["$check:assert"]),
    ],
)
def test_a_pass_without_an_obligation_of_the_mode_fails(
    tmp_path, fake_sby, monkeypatch, mode, cells
):
    _spec(monkeypatch, cells=cells)
    flow = _run(tmp_path, _design(tmp_path), mode=mode)
    assert flow.outcome == "failed"
    assert "no " in _document(flow)["error"]["message"]


@pytest.mark.parametrize("status", ["PASS", "FAIL"])
@pytest.mark.parametrize(
    "cell", ["$anyconst", "$anyseq", "$allconst", "$allseq", "$live", "$check:live", "$fair"]
)
def test_an_unsupported_formal_construct_fails_and_is_never_a_verdict(
    tmp_path, fake_sby, monkeypatch, status, cell
):
    _spec(monkeypatch, status=status, cells=["$check:assert", cell])
    flow = _run(tmp_path, _design(tmp_path))
    assert flow.outcome == "failed" and flow.rejection is None
    assert "unsupported" in _document(flow)["error"]["message"]


def test_a_pass_without_a_model_fails(tmp_path, fake_sby, monkeypatch):
    _spec(monkeypatch, model=False)
    assert _run(tmp_path, _design(tmp_path)).outcome == "failed"


def test_the_archive_holds_no_link_and_nothing_it_leads_to(tmp_path, fake_sby, monkeypatch):
    secret = tmp_path / "secret"
    secret.mkdir()
    (secret / "key.txt").write_text("secret\n")
    _spec(monkeypatch, link=str(secret))
    flow = _run(tmp_path, _design(tmp_path))
    assert flow.outcome == "passed"
    assert not any(name.startswith("outside") for name in _archived(flow))
    assert (secret / "key.txt").read_text() == "secret\n"


# ---- real tools ----------------------------------------------------------------------------


def _beside_sby(*names: str) -> list[str] | None:
    """The paths of `names` beside the `sby` on PATH, which the flow runs, or None."""
    sby = shutil.which("sby")
    paths = [Path(sby).parent / name for name in names] if sby else []
    return [str(p) for p in paths] if paths and all(os.access(p, os.X_OK) for p in paths) else None


@functools.cache
def _probe_sby_slang() -> bool:
    if not (programs := _beside_sby("yosys", "yosys-smtbmc", "bitwuzla")):
        return False
    with tempfile.TemporaryDirectory() as tmp:
        return tool_utils._command_succeeds([programs[0], "-p", "plugin -i slang"], cwd=tmp)


@functools.cache
def _probe_ghdl_formal() -> bool:
    """Elaborate a clocked VHDL assertion with PSL parsing through the plugin, and find it."""
    if not _probe_sby_slang():
        return False
    entity = (
        "entity p is port (clk, a : in bit); end;\narchitecture rtl of p is begin\n"
        "-- psl default clock is rising_edge(clk);\n-- psl assert always (a = '1');\nend;\n"
    )
    script = (
        "plugin -i ghdl; ghdl --std=08 -fpsl p.vhd -e p; "
        "select -assert-any t:$assert t:$check r:FLAVOR=assert %i"
    )
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "p.vhd").write_text(entity)
        return tool_utils._command_succeeds([*(_beside_sby("yosys") or []), "-p", script], cwd=tmp)


def require_sby() -> None:
    tool_utils._require(
        "sby with yosys-slang and bitwuzla", _probe_sby_slang(), "`yosys -p 'plugin -i slang'`"
    )


@functools.cache
def _probe_ric3() -> bool:
    programs = _beside_sby("rIC3", "yosys-witness", "btorsim")
    return bool(programs) and tool_utils._command_succeeds(
        [programs[0], "--version"], cwd=tempfile.gettempdir()
    )


def require_sby_ric3(mode: str = "prove") -> None:
    """Skip a `prove` test without rIC3; the other modes need only `sby` and Bitwuzla."""
    require_sby()
    if mode == "prove":
        tool_utils._require(
            "rIC3, yosys-witness and btorsim beside sby", _probe_ric3(), "`rIC3 --version`"
        )


def require_sby_ghdl() -> None:
    require_sby()
    tool_utils._require(
        "the yosys ghdl plugin with formal elaboration",
        _probe_ghdl_formal(),
        "elaborating a PSL assertion through `plugin -i ghdl`",
    )


#: rIC3 writes here, outside `TMPDIR` and never cleaned, when it runs its default portfolio. The
#: single-engine line of `prove` must not touch it. Reading it is no write; a portfolio run of
#: another user during the test would show here as well.
RIC3_SCRATCH = Path("/tmp/rIC3")


@pytest.fixture
def ric3_scratch():
    def names() -> list[str]:
        return sorted(p.name for p in RIC3_SCRATCH.iterdir()) if RIC3_SCRATCH.is_dir() else []

    before = names()
    yield
    assert names() == before, "rIC3 wrote to /tmp/rIC3"


def _ric3_processes_in(run_path: Path) -> list[int]:
    """The processes of rIC3 that still run with the work directory of this run as their cwd."""
    found = []
    for process in psutil.process_iter():
        try:
            if "rIC3" in " ".join(process.cmdline()) and Path(process.cwd()).is_relative_to(
                run_path.resolve()
            ):
                found.append(process.pid)
        except (psutil.Error, OSError):
            continue
    return found


def _real(tmp_path: Path, sources: dict[str, str], name: str = "d", **settings: Any):
    root = tmp_path / name
    root.mkdir(exist_ok=True)
    design = _design(root, sources, **settings.pop("rtl", {}))
    settings.setdefault("mode", "bmc")
    if settings["mode"] == "prove":
        settings.setdefault("timeout", "2 min")  # rIC3 proves without a bound: the task needs one
    else:
        settings.setdefault("depth", 6)
    flow = DefaultRunner(tmp_path / "xeda_run", display_results=False).run_flow(
        Sby, design, settings
    )
    assert flow is not None
    return flow


COUNTER = """module top #(parameter LIMIT = 9)(input clk);
  reg [3:0] count = 0;
  always @(posedge clk) count <= (count == LIMIT) ? 0 : count + 1;
  always @(posedge clk) assert(count <= LIMIT);
endmodule
"""


@pytest.mark.real_tool
@pytest.mark.parametrize("mode", ["bmc", "prove"])
def test_real_systemverilog_assertion_passes(tmp_path, ric3_scratch, mode):
    require_sby_ric3(mode)
    flow = _real(tmp_path, {"top.sv": COUNTER}, mode=mode)
    assert flow.outcome == "passed", _document(flow).get("error")
    assert flow.results["formal.status"] == "PASS"
    assert flow.results["formal.assertions"] == 1
    assert not (flow.run_path / "sby_tmp").exists()


@pytest.mark.real_tool
def test_real_variables_and_earlier_path_entries_do_not_change_the_programs(tmp_path, monkeypatch):
    """SBY reads `YOSYS` and `SMTBMC` before PATH. Here they and the first PATH entry name
    programs that fail, and the run still uses the programs beside `sby`."""
    require_sby()
    beside = Path(shutil.which("sby") or "").parent
    decoys = tmp_path / "decoys"
    decoys.mkdir()
    for name in ("yosys", "yosys-smtbmc", "bitwuzla"):
        _executable(decoys / name, "#!/bin/sh\nexit 1\n")
    monkeypatch.setenv("PATH", os.pathsep.join([str(decoys), os.environ["PATH"]]))
    monkeypatch.setenv("YOSYS", str(decoys / "yosys"))
    monkeypatch.setenv("SMTBMC", str(decoys / "yosys-smtbmc"))
    flow = _real(tmp_path, {"top.sv": COUNTER})
    assert flow.outcome == "passed", _document(flow).get("error")
    assert {Path(tool["executable"]).parent for tool in flow.results.tools} == {beside}


@pytest.mark.real_tool
def test_real_parameter_of_the_top_flips_the_verdict(tmp_path):
    require_sby()
    source = (
        "module top #(parameter OK = 1)(input clk); always @(posedge clk) assert(OK); endmodule\n"
    )
    good = _real(tmp_path, {"top.sv": source}, name="good", rtl={"parameters": {"OK": 1}})
    bad = _real(tmp_path, {"top.sv": source}, name="bad", rtl={"parameters": {"OK": 0}})
    assert good.outcome == "passed", _document(good).get("error")
    assert bad.outcome == "rejected" and "assertion fails" in bad.rejection
    assert any(name.endswith(".vcd") for name in _archived(bad))


NESTED = """module sub #(parameter OK = 1)(input clk);
  always @(posedge clk) assert(OK);
endmodule
module top #(parameter W = 4)(input clk, input [W-1:0] a);
  sub u(.clk(clk));
  always @(posedge clk) assert(W > 0);
endmodule
"""


def _interface(flow) -> tuple[list[str], dict[str, int]]:
    """The modules of the model of `flow` and the ports of its top, by width (`design.il`)."""
    with zipfile.ZipFile(flow.run_path / flow.artifacts["evidence_archive"]) as archive:
        text = archive.read("model/design.il").decode()
    modules = re.findall(r"^module \\(\S+)$", text, re.M)
    wires = re.findall(r"^ *wire (?:width (\d+) )?(?:input|output|inout) \d+ \\(\S+)$", text, re.M)
    return modules, {name: int(width or 1) for width, name in wires}


@pytest.mark.real_tool
def test_real_a_parameter_that_the_top_does_not_have_fails_and_is_named(tmp_path):
    """`OK` belongs to `sub`, not to the top, and `Ok` and `NOPE` are no parameters: none of them
    may prove the design with a default. Valid names change only their values: the model has the
    same modules, ports and assertions as without parameters."""
    require_sby()
    models = []
    for name, parameters in [("plain", {}), ("valid", {"W": 3})]:
        flow = _real(tmp_path, {"top.sv": NESTED}, name=name, rtl={"parameters": parameters})
        assert flow.outcome == "passed", _document(flow).get("error")
        # The run directory is shared, so read the model now.
        models.append((*_interface(flow), flow.results["formal.assertions"]))
    assert models == [(["top"], {"clk": 1, "a": 4}, 1), (["top"], {"clk": 1, "a": 3}, 1)]
    for name, parameters, unknown in [
        ("nested", {"OK": 0}, ["OK"]),
        ("several", {"W": 3, "Ok": 0, "NOPE": 1}, ["Ok", "NOPE"]),
    ]:
        flow = _real(tmp_path, {"top.sv": NESTED}, name=name, rtl={"parameters": parameters})
        error = _document(flow)["error"]
        assert flow.outcome == "failed" and error["code"] == "nonzero_exit"
        assert f"parameter '{unknown[0]}' does not exist in 'top'" in error["message"]
        log = (flow.run_path / "sby" / "model" / "design.log").read_text()
        assert all(f"parameter '{each}' does not exist in 'top'" in log for each in unknown)


@pytest.mark.real_tool
def test_real_cover_reaches_every_goal_and_reports_an_unreached_one(tmp_path):
    require_sby()
    reached = """module top(input clk, input a);
  reg [1:0] n = 0;
  always @(posedge clk) begin n <= n + a; cover(n == 2); cover(n == 3); end
endmodule
"""
    unreached = "module top(input clk); reg q = 0; always @(posedge clk) cover(q); endmodule\n"
    flow = _real(tmp_path, {"top.sv": reached}, name="reached", mode="cover")
    assert flow.outcome == "passed", _document(flow).get("error")
    assert flow.results["formal.covers"] == 2
    assert sum(name.endswith(".vcd") for name in _archived(flow)) >= 2
    flow = _real(tmp_path, {"top.sv": unreached}, name="unreached", mode="cover")
    assert flow.outcome == "rejected" and "not reached" in flow.rejection


@pytest.mark.real_tool
@pytest.mark.parametrize(
    "source, mode, reason",
    [
        ("module top(input clk, input a, output q); assign q = a; endmodule\n", "bmc", "no assert"),
        (
            "module top(input clk, input a); always @(posedge clk) assume(a); endmodule\n",
            "bmc",
            "no assert",
        ),
        (
            "module top(input clk, input a, output q); assign q = a; endmodule\n",
            "prove",
            "no assert",
        ),
        (SV_TOP, "cover", "no cover"),
        (
            "module top(input clk); (* anyconst *) reg a; reg v = 0;\n"
            "always @(posedge clk) begin v <= 1; if (v) assert(a == $past(a)); end endmodule\n",
            "bmc",
            "unsupported construct",
        ),
        (
            "module top(input clk, input a); always @(posedge clk) begin assume(a); "
            "assume(!a); assert(a); end endmodule\n",
            "bmc",
            "unsatisfiable",
        ),
        (
            "module top(input clk, input a, input b); "
            "assert property (@(posedge clk) a |-> ##3 b); endmodule\n",
            "bmc",
            "unsupported SVA",
        ),
    ],
    ids=[
        "no property",
        "assumptions only",
        "no property to prove",
        "no cover goal",
        "anyconst",
        "contradictory assumptions",
        "delayed implication",
    ],
)
def test_real_input_without_a_checkable_theorem_fails(tmp_path, source, mode, reason):
    require_sby_ric3(mode)
    flow = _real(tmp_path, {"top.sv": source}, mode=mode)
    assert flow.outcome == "failed" and flow.rejection is None
    assert flow.results.get("formal.status") == "ERROR"
    assert reason in _document(flow)["error"]["message"]


LATE_FAILURE = """module top(input clk);
  reg [3:0] count = 0;
  always @(posedge clk) if (count != 15) count <= count + 1;
  always @(posedge clk) assert(count != 6);
  always @(posedge clk) assert(count != 12);
endmodule
"""


@pytest.mark.real_tool
def test_real_bmc_passes_only_within_its_bound(tmp_path):
    """The assertion fails at the seventh step: a shorter bound passes, a longer one rejects."""
    require_sby()
    short = _real(tmp_path, {"top.sv": LATE_FAILURE}, name="short", depth=4)
    long = _real(tmp_path, {"top.sv": LATE_FAILURE}, name="long", depth=10)
    assert short.outcome == "passed", _document(short).get("error")
    assert long.outcome == "rejected" and "within depth 10" in str(long.rejection)


@pytest.mark.real_tool
def test_real_prove_finds_the_invariant_that_k_induction_does_not(tmp_path, ric3_scratch):
    """`count != 12` holds in every reachable state, but k-induction from the unreachable state 11
    cannot close at any short depth. IC3 strengthens the property by itself and proves it."""
    require_sby_ric3()
    source = """module top(input clk);
  reg [3:0] count = 0;
  always @(posedge clk) count <= (count == 9) ? 0 : count + 1;
  always @(posedge clk) assert(count != 12);
endmodule
"""
    flow = _real(tmp_path, {"top.sv": source}, mode="prove")
    assert flow.outcome == "passed", _document(flow).get("error")
    assert flow.results["formal.status"] == "PASS" and "formal.depth" not in flow.results
    assert (flow.results["formal.engine"], flow.results["formal.solver"]) == ("btor", "rIC3")
    assert "btor rIC3 -e ic3" in _config(flow)


@pytest.mark.real_tool
def test_real_prove_rejects_a_failing_assertion_with_its_witness(tmp_path, ric3_scratch):
    require_sby_ric3()
    flow = _real(tmp_path, {"top.sv": LATE_FAILURE}, mode="prove")
    assert flow.outcome == "rejected", _document(flow).get("error")
    assert "an assertion fails" in flow.rejection and "depth" not in flow.rejection
    assert flow.results["formal.status"] == "FAIL"
    assert {"engine_0/trace.vcd", "engine_0/trace.yw", "engine_0/trace.wit"} <= _archived(flow)
    assert _document(flow)["error"]["code"] == "rejected"


MEMORY = """module top(input clk, input we, input waddr, input raddr, input [7:0] wdata);
  reg [7:0] mem [0:1];
  reg [7:0] s0 = 8'd0, s1 = 8'd0;
  initial begin mem[0] = 8'd0; mem[1] = 8'd0; end
  always @(posedge clk) if (we) begin
    mem[waddr] <= wdata;
    if (waddr) s1 <= wdata%s; else s0 <= wdata;
  end
  always @* assert (mem[raddr] == (raddr ? s1 : s0));
endmodule
"""


@pytest.mark.real_tool
def test_real_prove_checks_a_memory_and_a_broken_memory_is_caught(tmp_path, ric3_scratch):
    """A scoreboard says that a read of either address returns the last write to it. The control
    shadows address 1 with a wrong value: the same property then fails, so it checks the array."""
    require_sby_ric3()
    good = _real(tmp_path, {"top.sv": MEMORY % ""}, name="good", mode="prove")
    broken = _real(tmp_path, {"top.sv": MEMORY % " + 8'd1"}, name="broken", mode="prove")
    assert good.outcome == "passed", _document(good).get("error")
    assert broken.outcome == "rejected" and "an assertion fails" in broken.rejection
    assert "engine_0/trace.yw" in _archived(broken)


@pytest.mark.real_tool
def test_real_prove_ends_at_the_task_timeout_without_a_leftover(tmp_path, ric3_scratch):
    """The assertion fails only at step 2**31, so no proof or counterexample comes in time. The
    SBY timeout ends the task, the run fails (a timeout proves nothing), and no rIC3 process, no
    owned temporary directory and nothing under `/tmp/rIC3` is left."""
    require_sby_ric3()
    source = """module top(input clk);
  reg [31:0] cnt = 32'd0;
  always @(posedge clk) cnt <= cnt + 32'd1;
  always @* assert (cnt != 32'h7fffffff);
endmodule
"""
    flow = _real(tmp_path, {"top.sv": source}, mode="prove", timeout="3 s")
    assert flow.outcome == "failed" and flow.rejection is None
    assert flow.results["formal.status"] == "TIMEOUT"
    error = _document(flow)["error"]
    assert error["code"] == "nonzero_exit" and "TIMEOUT" in error["message"]
    assert not (flow.run_path / "sby_tmp").exists()
    assert "logfile.txt" in _archived(flow)
    assert _ric3_processes_in(flow.run_path) == []


@pytest.mark.real_tool
def test_real_prove_has_no_presat_check_so_contradictory_assumptions_pass_vacuously(
    tmp_path, ric3_scratch
):
    """rIC3 does not check the assumptions for satisfiability. With `assume(a); assume(!a)` no
    state is reachable, so `assert(a)` passes for no reason. The counts of the results do not show
    it: `formal.assumptions` is 2 and `formal.assertions` is 1. `bmc` (smtbmc) does check, and
    fails the same source (`test_real_input_without_a_checkable_theorem_fails`)."""
    require_sby_ric3()
    source = (
        "module top(input clk, input a); always @(posedge clk) begin assume(a); assume(!a); "
        "assert(a); end endmodule\n"
    )
    flow = _real(tmp_path, {"top.sv": source}, mode="prove")
    assert flow.outcome == "passed", _document(flow).get("error")
    assert (flow.results["formal.assumptions"], flow.results["formal.assertions"]) == (2, 1)


@pytest.mark.real_tool
def test_real_tautology_is_a_valid_proof(tmp_path):
    require_sby()
    source = "module top(input clk, input a); always @(posedge clk) assert(a == a); endmodule\n"
    flow = _real(tmp_path, {"top.sv": source})
    assert flow.outcome == "passed", _document(flow).get("error")


@pytest.mark.real_tool
def test_real_formal_branch_and_staged_layout(tmp_path):
    """`FORMAL` is defined and `SYNTHESIS` is not; a relative include, a header of the same name
    in another directory and a memory file read by a path relative to the design root all reach
    the proof. The design root and the run root have spaces."""
    require_sby()
    sources = {
        "inc/value.svh": "`define VALUE 8'h5a\n",
        "other/inc/value.svh": "`define VALUE 8'h00\n",
        "mem/init.hex": "5a\n",
        "rtl/top.sv": """`include "../inc/value.svh"
module top(input clk);
  reg [7:0] mem [0:0];
  initial $readmemh("mem/init.hex", mem);
`ifdef SYNTHESIS
  missing_module never();
`endif
`ifdef FORMAL
  always @(posedge clk) assert(mem[0] == `VALUE);
`endif
endmodule
""",
    }
    root = tmp_path / "my design"
    root.mkdir()
    design = _design(root, sources)
    flow = DefaultRunner(tmp_path / "run root", display_results=False).run_flow(
        Sby, design, {"mode": "bmc", "depth": 3}
    )
    assert flow is not None
    assert flow.outcome == "passed", _document(flow).get("error")
    assert flow.results["formal.assertions"] == 1


VHDL_ASSERT = """entity top is
  generic (OK : boolean := true);
  port (clk, a : in bit);
end;
architecture rtl of top is
begin
  process (clk) begin
    if rising_edge(clk) then
      assert OK severity error;
    end if;
  end process;
end;
"""

VHDL_PSL = """entity top is port (clk : in bit); end;
architecture rtl of top is
  signal q : bit := '0';
begin
  process (clk) begin
    if rising_edge(clk) then q <= not q; end if;
  end process;
  -- psl default clock is rising_edge(clk);
  -- psl assert always (q = '0' or q = '1');
  -- psl cover {q = '1'};
end;
"""


@pytest.mark.real_tool
def test_real_vhdl_assertion_and_generic_flip_the_verdict(tmp_path):
    require_sby_ghdl()
    vhdl = {"language": {"vhdl": {"standard": "08"}}}
    good = _real(tmp_path, {"top.vhd": VHDL_ASSERT}, name="good", rtl=dict(vhdl))
    bad = _real(
        tmp_path, {"top.vhd": VHDL_ASSERT}, name="bad", rtl={**vhdl, "parameters": {"OK": False}}
    )
    assert good.outcome == "passed", _document(good).get("error")
    assert bad.outcome == "rejected" and "assertion fails" in bad.rejection
    # GHDL refuses a generic that the entity does not have, so it needs no second root.
    unknown = _real(
        tmp_path, {"top.vhd": VHDL_ASSERT}, name="unknown", rtl={**vhdl, "parameters": {"NOPE": 0}}
    )
    assert unknown.outcome == "failed"
    assert 'no generic "nope"' in _document(unknown)["error"]["message"]


@pytest.mark.real_tool
@pytest.mark.parametrize("mode", ["bmc", "prove", "cover"])
def test_real_vhdl_psl_in_comments(tmp_path, ric3_scratch, mode):
    require_sby_ghdl()
    require_sby_ric3(mode)
    flow = _real(
        tmp_path, {"top.vhd": VHDL_PSL}, mode=mode, rtl={"language": {"vhdl": {"standard": "08"}}}
    )
    assert flow.outcome == "passed", _document(flow).get("error")
