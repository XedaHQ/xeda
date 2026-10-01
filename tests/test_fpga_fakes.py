"""Process fakes must consume inputs and obey the tool's output and failure contracts."""

import json
import os
import signal
import shutil
import struct
import subprocess
import sys
import time
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner

from . import tool_utils

TOOLS = (
    "nextpnr-himbaechel",
    "nextpnr-ecp5",
    "nextpnr-ice40",
    "nextpnr-nexus",
    "fpga-as",
    "ecppack",
    "icepack",
    "openFPGALoader",
)


def _run(name, args, cwd, env=None):
    executable = tool_utils.FAKE_TOOLS_DIR / name
    assert executable.is_file(), f"missing fake {name}; never fall back to PATH"
    return subprocess.run(
        [str(executable), *map(str, args)],
        cwd=cwd,
        env={**os.environ, **(env or {})},
        capture_output=True,
        timeout=10,
    )


def _calls(root):
    return [json.loads(line) for line in (root / "fake_fpga.calls.jsonl").read_text().splitlines()]


def _inputs(root):
    netlist = root / "input with spaces.json"
    netlist.write_text('{"modules": {"top": {"ports": {}, "cells": {}}}}\n')
    chipdb = root / "input with spaces.bin"
    chipdb.write_bytes(b"chipdb input")
    return netlist, chipdb


def test_fake_yosys_resolves_library_pseudo_paths_from_install_prefix(tmp_path, monkeypatch):
    prefix = tool_utils.use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")
    source = tmp_path / "top.v"
    source.write_text("module top(); endmodule\n")
    script = tmp_path / "build.ys"
    script.write_text(
        "read_verilog -lib +/xilinx/cells_sim.v\n"
        "read_verilog -defer top.v\n"
        "hierarchy -check -top top\n"
        "write_json netlist.json\n"
    )
    result = subprocess.run(
        [str(prefix / "bin/yosys"), "-s", str(script)],
        cwd=tmp_path,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    (call,) = _calls(tmp_path)
    assert call["inputs"] == [
        str(script),
        str(prefix / "share/yosys/xilinx/cells_sim.v"),
        source.name,
    ]


@pytest.mark.parametrize("name", TOOLS)
@pytest.mark.parametrize("probe", ["--version", "--help"])
def test_probes_do_not_build_or_record_an_action(name, probe, tmp_path):
    result = _run(name, [probe], tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout or result.stderr
    assert not list(tmp_path.iterdir())


@pytest.mark.parametrize(
    "name,config_flag,device",
    [
        ("nextpnr-ecp5", "--textcfg", ["--25k", "--package=CABGA381"]),
        ("nextpnr-ice40", "--asc", ["--hx1k", "--package=tq144"]),
        ("nextpnr-nexus", "--fasm", ["--device=LIFCL-40-9BG400C"]),
        ("nextpnr-himbaechel", None, ["--device", "xc7a100tcsg324-1"]),
    ],
)
def test_nextpnr_consumes_inputs_and_writes_only_named_outputs(name, config_flag, device, tmp_path):
    netlist, chipdb = _inputs(tmp_path)
    args = [f"--json={netlist}", *device, "--seed=7", "--report=report with spaces.json"]
    if config_flag:
        args += [config_flag, "config with spaces.txt"]
    else:
        args += [
            "--chipdb",
            chipdb,
            "-o",
            "fasm=config with spaces.txt",
            "-o",
            "placement=placement with spaces.json",
            "-l",
            "nextpnr.log",
            "-q",
        ]
    args += ["--write", "placed with spaces.json"]
    result = _run(name, args, tmp_path, {"PYTHONDONTWRITEBYTECODE": "1"})
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "config with spaces.txt").read_bytes()
    assert json.loads((tmp_path / "report with spaces.json").read_text())["utilization"]
    assert json.loads((tmp_path / "placed with spaces.json").read_text())["modules"]
    if not config_flag:
        assert json.loads((tmp_path / "placement with spaces.json").read_text())["lut"] == {
            "tile": "CLBLL_L_X0Y0",
            "site": "SLICE_X0Y0",
            "bel": "A6LUT",
            "type": "SLICE_LUTX",
        }
    (call,) = _calls(tmp_path)
    assert call["tool"] == name
    assert call["argv"] == list(map(str, args))
    assert Path(call["cwd"]).resolve() == tmp_path.resolve()
    assert call["environment"]["PYTHONDONTWRITEBYTECODE"] == "1"
    assert call["seed"] == 7
    assert set(call["inputs"]) == {str(netlist), *([str(chipdb)] if not config_flag else [])}
    expected_files = {
        netlist.name,
        chipdb.name,
        "config with spaces.txt",
        "report with spaces.json",
        "placed with spaces.json",
        "fake_fpga.calls.jsonl",
    }
    if not config_flag:
        expected_files.update(("placement with spaces.json", "nextpnr.log"))
    assert set(p.name for p in tmp_path.iterdir()) == expected_files


@pytest.mark.parametrize("name", ["ecppack", "icepack"])
def test_packers_read_configuration_and_use_positional_output(name, tmp_path):
    source = tmp_path / "input config"
    source.write_text("configuration\n")
    result = _run(name, [source, "packed output.bin"], tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / "packed output.bin").read_bytes() == b"\x00\xffXEDA bitstream\x00"
    assert _calls(tmp_path)[0]["inputs"] == [str(source)]
    assert source.read_text() == "configuration\n"


def _fpga_as_args(root):
    database = root / "database"
    database.mkdir()
    source = root / "input.fasm"
    source.write_text("TILE.FEATURE\n")
    return [f"--prjxray_db_path={database}", "--part", "xc7a100tcsg324-1", source]


def test_fpga_as_stdout_is_binary_and_diagnostics_are_separate(tmp_path):
    result = _run("fpga-as", _fpga_as_args(tmp_path), tmp_path)
    assert result.returncode == 0, result.stderr
    assert result.stdout == b"\x00\xffXEDA bitstream\x00"
    assert b"fake fpga-as" in result.stderr
    assert set(p.name for p in tmp_path.iterdir()) == {
        "database",
        "input.fasm",
        "fake_fpga.calls.jsonl",
    }


@pytest.mark.parametrize(
    "mode,code,output", [("partial", 1, b"\x00\xffpartial"), ("no-output", 0, b"")]
)
def test_fpga_as_failure_modes(mode, code, output, tmp_path):
    result = _run("fpga-as", _fpga_as_args(tmp_path), tmp_path, {"XEDA_FAKE_FPGA_MODE": mode})
    assert result.returncode == code
    assert result.stdout == output


@pytest.mark.parametrize(
    "name", ["ecppack", "icepack", "openFPGALoader", "nextpnr-ecp5", "fpga-as"]
)
def test_missing_inputs_cannot_produce_or_program_anything(name, tmp_path):
    args = {
        "ecppack": ["missing", "output.bit"],
        "icepack": ["missing", "output.bin"],
        "openFPGALoader": ["--bitstream", "missing.bit"],
        "nextpnr-ecp5": ["--25k", "--json=missing.json", "--textcfg=output.cfg"],
        "fpga-as": ["--prjxray_db_path", tmp_path, "--part", "xc7a100tcsg324-1", "missing.fasm"],
    }[name]
    result = _run(name, args, tmp_path)
    assert result.returncode != 0
    assert b"missing" in result.stderr
    assert not result.stdout
    assert not list(tmp_path.iterdir()), "a fake must open inputs before producing outputs"


def test_loader_explicit_dispatch_reads_a_typed_bitstream(tmp_path, monkeypatch):
    path = tmp_path / "prebuilt.bin"
    path.write_bytes(b"\xff\x00prebuilt")
    design = Design(name="prebuilt", rtl={"sources": [{"file": path, "type": "Bitstream"}]})
    tool_utils.use_fake_tools(monkeypatch)
    assert (
        Path(shutil.which("openFPGALoader")).resolve()
        == (tool_utils.FAKE_TOOLS_DIR / "fake_fpga_tool.py").resolve()
    )
    args = [
        "--bitstream",
        design.rtl.sources[0].path,
        "--board",
        "ulx3s",
        "--write-flash",
        "--verify",
    ]
    result = _run("openFPGALoader", args, tmp_path)
    assert result.returncode == 0, result.stderr
    (call,) = _calls(tmp_path)
    assert call["inputs"] == [str(path)]
    assert call["input_bytes"] == [len(path.read_bytes())]


def test_missing_fake_loader_fails_before_path_fallback(tmp_path, monkeypatch):
    original = os.environ["PATH"]
    monkeypatch.setattr(tool_utils, "FAKE_TOOLS_DIR", tmp_path)
    with pytest.raises(AssertionError, match="openFPGALoader"):
        tool_utils.use_fake_tools(monkeypatch)
    assert os.environ["PATH"] == original


def test_nonexecutable_fake_loader_fails_before_path_fallback(tmp_path, monkeypatch):
    dispatcher = tmp_path / "fake_fpga_tool.py"
    dispatcher.write_text("# fake without execute permissions\n")
    dispatcher.chmod(0o644)
    (tmp_path / "openFPGALoader").symlink_to(dispatcher.name)
    monkeypatch.setattr(tool_utils, "FAKE_TOOLS_DIR", tmp_path)
    with pytest.raises(AssertionError, match="openFPGALoader"):
        tool_utils.use_fake_tools(monkeypatch)


def test_existing_malformed_netlist_cannot_be_routed(tmp_path):
    netlist = tmp_path / "bad.json"
    netlist.write_text("not a netlist")
    result = _run("nextpnr-ecp5", ["--25k", "--json", netlist, "--textcfg=cfg"], tmp_path)
    assert result.returncode != 0
    assert not (tmp_path / "cfg").exists()
    assert not (tmp_path / "fake_fpga.calls.jsonl").exists()


@pytest.mark.parametrize("mode", ["no-output", "malformed-report", "signal"])
def test_nextpnr_failure_modes(mode, tmp_path):
    netlist, _ = _inputs(tmp_path)
    result = _run(
        "nextpnr-ecp5",
        ["--25k", "--json", netlist, "--report=report.json", "--textcfg=config.txt"],
        tmp_path,
        {"XEDA_FAKE_FPGA_MODE": mode},
    )
    if mode == "signal":
        assert result.returncode == -signal.SIGTERM
    else:
        assert result.returncode == 0
    if mode == "malformed-report":
        with pytest.raises(json.JSONDecodeError):
            json.loads((tmp_path / "report.json").read_text())
    else:
        assert not (tmp_path / "report.json").exists()
        assert not (tmp_path / "config.txt").exists()


def test_a_delayed_step_records_before_waiting(tmp_path):
    path = tmp_path / "prebuilt.bit"
    path.write_bytes(b"packed")
    start = time.monotonic()
    result = _run(
        "openFPGALoader", ["--bitstream", path], tmp_path, {"XEDA_FAKE_FPGA_DELAY": "0.1"}
    )
    assert result.returncode == 0
    assert time.monotonic() - start >= 0.1
    assert len(_calls(tmp_path)) == 1


def test_fake_prefix_models_generator_and_assembler(tmp_path, monkeypatch):
    prefix = tool_utils.use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")
    probe = subprocess.run(
        [str(prefix / "bin/yosys"), "-T", "-Q", "-V"],
        cwd=tmp_path,
        capture_output=True,
        timeout=10,
    )
    assert probe.returncode == 0 and b"Yosys 0.63" in probe.stdout, probe.stderr
    assert not (tmp_path / "fake_fpga.calls.jsonl").exists()
    share = prefix / "share/nextpnr"
    generator = share / "himbaechel/uarch/xilinx/gen/xilinx_gen.py"
    args = [
        sys.executable,
        generator,
        "--xray",
        share / "prjxray-db/artix7",
        "--device",
        "xc7a100t",
        "--bba",
        "chip.bba",
    ]
    result = subprocess.run(list(map(str, args)), cwd=tmp_path, capture_output=True, timeout=10)
    assert result.returncode == 0, result.stderr
    result = subprocess.run(
        [str(prefix / "bin/bbasm"), "-l", "chip.bba", "chip.bin"],
        cwd=tmp_path,
        capture_output=True,
        timeout=10,
    )
    assert result.returncode == 0, result.stderr
    binary = (tmp_path / "chip.bin").read_bytes()
    offset = struct.unpack_from("<i", binary)[0]
    assert struct.unpack_from("<4I", binary, offset) == (0x00CA7CA7, 6, 2, 2)
    for i, expected in enumerate((b"xilinx", b"xc7a100t", b"python_dbgen")):
        pointer = offset + 16 + i * 4
        string = pointer + struct.unpack_from("<i", binary, pointer)[0]
        assert binary[string:].split(b"\0", 1)[0] == expected
    assert binary.endswith(b"xilinx\0xc7a100t\0python_dbgen\0")
    assert [call["tool"] for call in _calls(tmp_path)] == ["xilinx_gen.py", "bbasm"]


@pytest.mark.parametrize("part", ["LFE5U-25F-6BG381C", "iCE40HX1K-TQ144", "LIFCL-40-9BG400C"])
def test_process_handover_and_second_launch_reuse(part, tmp_path, monkeypatch):
    tool_utils.use_fake_fpga_tools(monkeypatch, tmp_path / "toolchain")
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "top.v"
    source.write_text("module top(input clk, output q); assign q = clk; endmodule\n")
    design = Design(name="top", rtl={"sources": [source], "top": "top"})
    runner = DefaultRunner(tmp_path / "run", display_results=False)
    first = runner.run("nextpnr", design, flow_settings={"fpga": part})
    assert first is not None and first.succeeded
    producer = first.completed_dependencies[0]
    call = _calls(first.run_path)[0]
    assert call["inputs"] == [str(first.inputs.netlist)]
    assert str(first.inputs.netlist) == producer.results["outputs"]["netlist"]["path"]
    assert first.outputs.config.read_bytes()
    again = runner.run("nextpnr", design, flow_settings={"fpga": part})
    assert again is not None and again.reused and again.completed_dependencies[0].reused
    assert len(_calls(first.run_path)) == len(_calls(producer.run_path)) == 1


@pytest.mark.parametrize("name", ["yosys_fpga", "nextpnr", "openfpgaloader"])
def test_fpga_isolation_launch_reaches_success(name, tmp_path, monkeypatch):
    from .test_isolation import _flow, _launch, _world

    world = _world(tmp_path)
    assert _launch(_flow(name), world, monkeypatch, "once", []) == "ok"
    calls = [
        call
        for path in world.root.glob("sqrt/*/fake_fpga.calls.jsonl")
        for call in _calls(path.parent)
    ]
    tools = {call["tool"] for call in calls}
    expected = {
        "yosys_fpga": {"yosys"},
        "nextpnr": {"yosys", "nextpnr-ecp5"},
        "openfpgaloader": {"yosys", "nextpnr-ecp5", "ecppack", "openFPGALoader"},
    }[name]
    assert tools == expected
