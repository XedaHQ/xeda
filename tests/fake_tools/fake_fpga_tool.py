#!/usr/bin/env python3
"""Small process models for FPGA tests; never invoke tools or access hardware.

Calls are JSON lines in the working directory, after every input has been opened.
XEDA_FAKE_FPGA_TOOL optionally limits fault injection to one executable. MODE is
partial, no-output, malformed-report, fail, or signal; DELAY is seconds before output.
Only version/help probes bypass input validation and call recording.
"""

import argparse
import json
import os
import shlex
import signal
import struct
import sys
import time
from pathlib import Path

BITSTREAM = b"\x00\xffXEDA bitstream\x00"
NETLIST = {"creator": "fake yosys", "modules": {"top": {"ports": {}, "cells": {}}}}
VALUE_OPTIONS = {
    "json",
    "chipdb",
    "device",
    "package",
    "speed",
    "freq",
    "top",
    "seed",
    "uarch",
    "textcfg",
    "asc",
    "fasm",
    "report",
    "write",
    "placement",
    "sdf",
    "log",
    "placed-svg",
    "routed-svg",
    "threads",
    "lpf",
    "pcf",
    "pdc",
    "xdc",
    "sdc",
    "pre-pack",
    "pre-place",
    "pre-route",
    "post-route",
    "run",
    "placer",
    "router",
    "cstrweight",
    "starttemp",
    "placer-heap-alpha",
    "placer-heap-beta",
    "placer-heap-critexp",
    "placer-heap-timingweight",
    "override-basecfg",
    "slack-redist-iter",
    "carry-lutff-ratio",
    "estimate-delay-mult",
    "bitstream",
    "board",
    "cable",
    "fpga-part",
    "offset",
    "cable-index",
    "usb-serial-num",
    "index-chain",
    "file-type",
    "target-flash",
    "verbose-level",
    "prjxray_db_path",
    "dump_frames_file",
    "part",
}


def options(args):
    """Keep values (including spaces and '=') as one argv element."""
    values, positional = {}, []
    iterator = iter(args)
    for arg in iterator:
        if arg in ("-o", "--vopt"):
            name, sep, value = next(iterator).partition("=")
            if not sep:
                raise ValueError("-o requires name=value")
            values[name] = value
        elif arg == "-l":
            values["log"] = next(iterator)
        elif arg in ("-q", "-v"):
            values[{"-q": "quiet", "-v": "verbose"}[arg]] = True
        elif arg.startswith("--"):
            name, sep, value = arg[2:].partition("=")
            if not sep:
                value = next(iterator) if name in VALUE_OPTIONS else True
            values[name] = value
        else:
            positional.append(arg)
    return values, positional


def read_inputs(paths):
    return [Path(path).read_bytes() for path in paths]


def record(tool, args, inputs, contents, seed=1):
    # Record only test controls and tool configuration, never arbitrary inherited secrets.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key.startswith("XEDA_FAKE_FPGA_")
        or key in ("PYTHONDONTWRITEBYTECODE", "PYTHONPYCACHEPREFIX", "PYTHONPATH")
    }
    call = {
        "tool": tool,
        "argv": args,
        "cwd": str(Path.cwd()),
        "environment": environment,
        "inputs": list(map(str, inputs)),
        "input_bytes": list(map(len, contents)),
        "seed": seed,
    }
    with open("fake_fpga.calls.jsonl", "a", encoding="utf-8") as stream:
        stream.write(json.dumps(call) + "\n")
    selected = os.environ.get("XEDA_FAKE_FPGA_TOOL")
    if selected and selected != tool:
        return ""
    time.sleep(float(os.environ.get("XEDA_FAKE_FPGA_DELAY", "0")))
    mode = os.environ.get("XEDA_FAKE_FPGA_MODE", "")
    if mode == "signal":
        os.kill(os.getpid(), signal.SIGTERM)
    if mode == "fail":
        raise ValueError(f"requested failure in {tool}")
    return mode


def write(path, content):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content if isinstance(content, bytes) else content.encode())


def chipdb(fabric):
    """A version-6 header with relative string pointers and the real EOF string marker.

    This is a structural validation fixture, not a routable chip database. Empty slices
    stand in for the large tables. The header starts at offset 4, not at the magic word.
    """
    header = bytearray(struct.pack("<4I", 0x00CA7CA7, 6, 2, 2) + bytes(72))
    strings = b"xilinx\0" + fabric.encode() + b"\0python_dbgen\0"
    start = 4 + len(header)
    for i, string in enumerate((b"xilinx", fabric.encode(), b"python_dbgen")):
        pointer = 4 + 16 + 4 * i
        struct.pack_into("<i", header, 16 + 4 * i, start - pointer)
        start += len(string) + 1
    return struct.pack("<i", 4) + header + strings


def nextpnr(tool, args):
    opts, positional = options(args)
    if positional:
        raise ValueError(f"unexpected arguments: {positional}")
    paths = [opts["json"]]
    if tool == "nextpnr-himbaechel":
        if not opts.get("device"):
            raise ValueError("missing --device")
        paths.append(opts["chipdb"])
    for key in (
        "lpf",
        "pcf",
        "pdc",
        "xdc",
        "sdc",
        "pre-pack",
        "pre-place",
        "pre-route",
        "post-route",
        "run",
    ):
        if opts.get(key):
            paths.append(opts[key])
    contents = read_inputs(paths)
    netlist = json.loads(contents[0])
    if not isinstance(netlist.get("modules"), dict):
        raise ValueError("JSON input needs modules")
    mode = record(tool, args, paths, contents, int(opts.get("seed", 1)))
    if mode == "no-output":
        return 0
    utilization = {
        "nextpnr-ecp5": {
            "TRELLIS_COMB": {"used": 1, "available": 24288},
            "TRELLIS_FF": {"used": 1, "available": 24288},
        },
        "nextpnr-ice40": {"ICESTORM_LC": {"used": 1, "available": 1280}},
        "nextpnr-nexus": {"OXIDE_COMB": {"used": 1, "available": 40000}},
        "nextpnr-himbaechel": {
            "SLICE_LUTX": {"used": 1, "available": 126800},
            "SLICE_FFX": {"used": 1, "available": 126800},
        },
    }[tool]
    report = {
        "fmax": {"clk": {"achieved": 250.0, "constraint": float(opts.get("freq", 100))}},
        "utilization": utilization,
        "critical_paths": [],
    }
    for key in ("textcfg", "asc", "fasm"):
        if opts.get(key):
            write(opts[key], "TILE.FEATURE\n" if key == "fasm" else "configuration\n")
    if opts.get("report"):
        write(opts["report"], "{malformed" if mode == "malformed-report" else json.dumps(report))
    for key in ("write", "placement"):
        if opts.get(key):
            placed = {
                "modules": {
                    "top": {
                        "ports": {},
                        "cells": {
                            "lut": {
                                "type": "SLICE_LUTX",
                                "attributes": {"NEXTPNR_BEL": "CLBLL_L_X0Y0/SLICE_X0Y0/A6LUT"},
                            }
                        },
                    }
                }
            }
            if key == "placement":
                placed = {
                    "lut": {
                        "tile": "CLBLL_L_X0Y0",
                        "site": "SLICE_X0Y0",
                        "bel": "A6LUT",
                        "type": "SLICE_LUTX",
                    }
                }
            write(opts[key], json.dumps(placed))
    for key in ("sdf", "log", "placed-svg", "routed-svg"):
        if opts.get(key):
            write(opts[key], f"fake {key}\n")
    return 0


def pack_or_load(tool, args):
    opts, positional = options(args)
    if tool == "openFPGALoader":
        paths = [opts.get("bitstream") or (positional[0] if positional else "")]
    elif tool == "fpga-as":
        if not Path(opts["prjxray_db_path"]).is_dir() or not opts.get("part"):
            raise ValueError("fpga-as requires --prjxray_db_path directory and --part")
        if len(positional) != 1:
            raise ValueError("fpga-as requires one FASM input")
        paths = positional
    else:
        if len(positional) != 2:
            raise ValueError(f"{tool} requires configuration and output")
        paths = positional[:1]
    contents = read_inputs(paths)
    if not all(contents):
        raise ValueError("empty input")
    mode = record(tool, args, paths, contents)
    if mode == "no-output" or tool == "openFPGALoader":
        return 0
    output = b"\x00\xffpartial" if mode == "partial" else BITSTREAM
    if tool == "fpga-as":
        print("fake fpga-as: packing", file=sys.stderr)
        sys.stdout.buffer.write(output)
        sys.stdout.buffer.flush()
        if opts.get("dump_frames_file"):
            write(opts["dump_frames_file"], "0x00000000 0x00000000\n")
    else:
        write(positional[1], output)
    return 1 if mode == "partial" else 0


def generate(tool, args):
    parser = argparse.ArgumentParser()
    for key in ("xray", "device", "bba"):
        parser.add_argument("--" + key, required=True)
    parser.add_argument("--metadata")
    parser.add_argument("--constids")
    opts = parser.parse_args(args)
    root = Path(opts.xray)
    paths = [root / "mapping/devices.yaml", root / opts.device / "tilegrid.json"]
    if opts.constids:
        paths.append(Path(opts.constids))
    contents = read_inputs(paths)
    mode = record(tool, args, paths, contents)
    if mode != "no-output":
        # The fake assembler reads this recipe; real BBA assembly belongs to real-tool gates.
        write(opts.bba, json.dumps({"fabric": opts.device}))
    return 0


def assemble(tool, args):
    if len(args) != 3 or args[0] != "-l":
        raise ValueError("bbasm requires -l input.bba output.bin")
    paths = [args[1]]
    contents = read_inputs(paths)
    fabric = json.loads(contents[0])["fabric"]
    mode = record(tool, args, paths, contents)
    if mode != "no-output":
        write(args[2], chipdb(fabric))
    return 0


def yosys(tool, args):
    """The .ys subset used by the isolation graph, without simulating synthesis."""
    if "-s" not in args:
        raise ValueError("fake yosys supports -s scripts only")
    script = Path(args[args.index("-s") + 1])
    lines = [
        shlex.split(line)
        for line in script.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    ]
    paths = [script]
    for words in lines:
        if words[0] in ("read_verilog", "read_vhdl"):
            for word in words[1:]:
                if word.startswith("+/"):
                    prefix = Path(sys.argv[0]).resolve().parent.parent
                    paths.append(prefix / "share/yosys" / word[2:])
                elif not word.startswith("-"):
                    paths.append(Path(word))
        elif words[0] == "ghdl":
            paths.extend(Path(word) for word in words[1:] if Path(word).suffix in (".vhd", ".vhdl"))
    contents = read_inputs(paths)
    mode = record(tool, args, paths, contents)
    if mode == "no-output":
        return 0
    for words in lines:
        if words[0] == "write_json":
            write(words[-1], json.dumps(NETLIST))
        elif words[0] in ("write_verilog", "write_blif"):
            write(words[-1], "module top(); endmodule\n")
        elif words[0] == "tee" and "-o" in words:
            output = words[words.index("-o") + 1]
            content = (
                json.dumps(
                    {"design": {"num_cells": 1, "num_cells_by_type": {"LUT1": 1}}, "modules": {}}
                )
                if "stat" in words
                else "fake check\n"
            )
            write(output, content)
    for flag in ("-E", "-L"):
        if flag in args:
            write(args[args.index(flag) + 1], "" if flag == "-E" else "fake yosys\n")
    return 0


def main():
    tool, args = Path(sys.argv[0]).name, sys.argv[1:]
    probe_args = [arg for arg in args if arg not in ("-T", "-Q")]
    if probe_args in (["--version"], ["-V"], ["--help"], ["-h"]):
        if tool.startswith("nextpnr-"):
            print(
                f'"{tool}" -- Next Generation Place and Route (Version nextpnr-1.0.0)',
                file=sys.stderr,
            )
        else:
            print("Yosys 0.63" if tool == "yosys" else f"{tool} 1.0.0 (fake)")
        return 0
    if tool.startswith("nextpnr-"):
        return nextpnr(tool, args)
    if tool in ("openFPGALoader", "fpga-as", "ecppack", "icepack"):
        return pack_or_load(tool, args)
    if tool == "xilinx_gen.py":
        return generate(tool, args)
    if tool == "bbasm":
        return assemble(tool, args)
    if tool == "yosys":
        return yosys(tool, args)
    raise ValueError(f"unknown fake FPGA tool {tool}")


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError, StopIteration) as error:
        print(f"fake {Path(sys.argv[0]).name}: {error}", file=sys.stderr)
        sys.exit(1)
