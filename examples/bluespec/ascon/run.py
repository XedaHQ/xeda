#!/usr/bin/env python3
"""Compile the Ascon BSV sources and hand their Verilog artifacts to Xeda flows."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import Bsc, BscSim, Openroad, Yosys, YosysFpga

DESIGN_FILE = Path(__file__).resolve().with_name("ascon.yaml")


def _validate_args(args: argparse.Namespace) -> None:
    factor = args.unroll_factor
    rounds = (12, 8 if args.ascon128a else 6)
    if factor <= 0 or any(count % factor for count in rounds):
        raise ValueError(
            "unroll factor must divide 12 and "
            f"{rounds[1]} for the selected AEAD mode (got {factor})"
        )


def _design(args: argparse.Namespace) -> Design:
    design = Design.from_file(DESIGN_FILE)
    factor = args.unroll_factor
    assert design.rtl is not None
    design.rtl.defines = {**design.rtl.defines, "UNROLL_FACTOR": factor}
    if args.ascon128a:
        design.rtl.defines["ASCON128A"] = True
    return design


def _success(flow: Any, label: str) -> bool:
    if flow is None or not flow.succeeded:
        print(f"{label} failed")
        return False
    print(f"{label}: {flow.run_path}")
    return True


def _handoff_design(design: Design, verilog: list[str | Path]) -> Design:
    """Build a synthesis design from the complete public `Bsc.artifacts.verilog` list."""
    assert design.rtl is not None and design.rtl.top and design.rtl.clock
    return Design(
        name=f"{design.name}_verilog",
        design_root=design.design_root,
        rtl={
            "sources": verilog,
            "top": design.rtl.top,
            "clock": design.rtl.clock.port,
        },
    )


def _asic_flow_settings(args: argparse.Namespace, clock: Any) -> dict[str, Any]:
    """Set the synthesis clock on its producer so OpenROAD placement inherits it."""
    settings: dict[str, Any] = {
        "platform": args.platform,
        "flows.yosys.clock.port": clock.port,
        "flows.yosys.clock.period": args.clock_period,
    }
    if args.pdk_root is not None:
        settings["pdk_root"] = args.pdk_root
    return settings


def _options(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--run-root", type=Path, default=Path("xeda_run"))
    parser.add_argument("--unroll-factor", type=int, default=1)
    parser.add_argument("--ascon128a", action="store_true")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)

    sim = commands.add_parser("sim", help="run the bsc_sim flow")
    _options(sim)
    sim.add_argument("--simulator", choices=("bluesim", "verilator", "iverilog"), default="bluesim")

    fpga = commands.add_parser("fpga", help="compile BSV, then run yosys_fpga")
    _options(fpga)
    fpga.add_argument("--part", default="xc7a100tftg256-2L")
    fpga.add_argument("--clock-period", default="10 ns")

    asic = commands.add_parser("asic", help="compile BSV, then run yosys or OpenROAD")
    _options(asic)
    asic.add_argument("--platform", default="nangate45")
    asic.add_argument("--clock-period", default="10 ns")
    asic.add_argument("--pdk-root", type=Path)
    asic.add_argument("--place-route", action="store_true")

    args = parser.parse_args()
    try:
        _validate_args(args)
    except ValueError as error:
        parser.error(str(error))
    design = _design(args)
    runner = DefaultRunner(args.run_root, display_results=False)

    if args.command == "sim":
        flow = runner.run(BscSim, design, flow_settings={"simulator": args.simulator})
        return 0 if _success(flow, "bsc_sim") else 1

    compiled = runner.run(Bsc, design)
    if not _success(compiled, "bsc"):
        return 1
    verilog = list(compiled.artifacts.verilog)
    if not verilog:
        print("bsc produced no Verilog artifacts")
        return 1
    netlist = _handoff_design(design, verilog)
    assert netlist.rtl is not None and netlist.rtl.clock
    print(
        f"Handoff: {len(verilog)} Verilog files, top={netlist.rtl.top}, "
        f"clock={netlist.rtl.clock.port}"
    )
    for path in verilog:
        print(f"  {path}")

    if args.command == "fpga":
        flow_settings = {
            "fpga": {"part": args.part},
            "clock": {"period": args.clock_period},
        }
        flow = runner.run(YosysFpga, netlist, flow_settings=flow_settings)
        return 0 if _success(flow, "yosys_fpga") else 1

    flow_settings = _asic_flow_settings(args, netlist.rtl.clock)
    flow_class = Openroad if args.place_route else Yosys
    flow = runner.run(flow_class, netlist, flow_settings=flow_settings)
    return 0 if _success(flow, flow_class.name) else 1


if __name__ == "__main__":
    raise SystemExit(main())
