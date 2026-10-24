"""Measure the complete UART transport tops against the logic LUT gate."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import YosysFpga

from .tool_utils import require_bsc, require_yosys, run_report

AREA = Path(__file__).resolve().parents[1] / "examples/bluespec/uart_link/apps/area.yaml"
ADAPTERS = [
    ("gcd_w8", 64, 32),
    ("gcd_w1", 64, 32),
    ("ascon40_w8", 40, 40),
    ("ascon40_w1", 40, 40),
]
BARE = [
    "leanlink_w8",
    "leanlink_w1",
]


def _port_widths(run_root: Path, top: str) -> dict[str, int]:
    generated = list(run_root.rglob(f"{top}.v"))
    assert len(generated) == 1, f"expected one generated Verilog top {top}, found {generated}"
    header = generated[0].read_text()
    return {
        name: int(width)
        for name, width in re.findall(r"^//\s+(\S+)\s+[IO]\s+(\d+)", header, re.MULTILINE)
    }


def _assert_memory_mapping(flow: YosysFpga, target: str) -> None:
    """Check the expected block RAM or distributed RAM mapping for this width profile."""
    results = flow.results
    utilization = results["_utilization"]
    assert results["LUT:RAM"] > 0
    if target.endswith("_w8"):
        block_rams = max(utilization.get("RAMB18", 0), utilization.get("RAMB18E1", 0))
        assert block_rams > 0
        assert utilization.get("RAM64M", 0) == 0
    else:
        assert utilization.get("RAMB18", 0) == 0
        assert utilization.get("RAM64M", 0) > 0


@pytest.mark.parametrize("target,input_width,output_width", ADAPTERS)
def test_uart_stream_adapter_area(target, input_width, output_width, tmp_path, capfd):
    """Keep both stream word widths and the full reset-aware adapter below 500 logic LUTs."""
    require_bsc()
    require_yosys()
    design = Design.from_file(AREA, target=target)
    run_root = tmp_path / target
    flow = DefaultRunner(run_root, display_results=False).run_flow(YosysFpga, design)
    captured = capfd.readouterr()
    assert flow is not None and flow.succeeded, run_report(flow, captured)
    assert flow.results["LUT:STAGE"] == "mapped"
    assert flow.results["LUT:METHOD"] == "primitive-footprint estimate"
    assert 0 <= flow.results["LUT:LOGIC"] <= 500
    assert flow.results["LUT:RAM"] >= 0
    assert flow.results["LUT:SRL"] >= 0
    assert flow.results["FF"] >= 0
    assert flow.results["CARRY4"] >= 0
    assert flow.results["LUT"] == (
        flow.results["LUT:LOGIC"] + flow.results["LUT:RAM"] + flow.results["LUT:SRL"]
    )
    _assert_memory_mapping(flow, target)
    ports = _port_widths(run_root, design.rtl.top)
    expected = {
        "CLK": 1,
        "RST_N": 1,
        "rx_put": 8,
        "EN_rx_put": 1,
        "RDY_rx_put": 1,
        "tx_get": 8,
        "EN_tx_get": 1,
        "RDY_tx_get": 1,
        "inputWords_get": input_width,
        "EN_inputWords_get": 1,
        "RDY_inputWords_get": 1,
        "outputWords_put": output_width,
        "EN_outputWords_put": 1,
        "RDY_outputWords_put": 1,
        "RST_N_coreReset": 1,
    }
    assert {name: ports.get(name) for name in expected} == expected


@pytest.mark.parametrize("target", BARE)
def test_leanlink_core_area(target, tmp_path, capfd):
    """Measure standalone LeanLink with its packet and control interfaces observable."""
    require_bsc()
    require_yosys()
    design = Design.from_file(AREA, target=target)
    run_root = tmp_path / target
    flow = DefaultRunner(run_root, display_results=False).run_flow(YosysFpga, design)
    captured = capfd.readouterr()
    assert flow is not None and flow.succeeded, run_report(flow, captured)
    assert flow.results["LUT:STAGE"] == "mapped"
    assert flow.results["LUT:METHOD"] == "primitive-footprint estimate"
    assert flow.results["LUT:LOGIC"] >= 0
    assert flow.results["LUT:RAM"] >= 0
    assert flow.results["LUT:SRL"] >= 0
    assert flow.results["FF"] >= 0
    assert flow.results["CARRY4"] >= 0
    assert flow.results["LUT"] == (
        flow.results["LUT:LOGIC"] + flow.results["LUT:RAM"] + flow.results["LUT:SRL"]
    )
    _assert_memory_mapping(flow, target)
    ports = _port_widths(run_root, design.rtl.top)
    expected = {
        "CLK": 1,
        "RST_N": 1,
        "rx_put": 8,
        "EN_rx_put": 1,
        "RDY_rx_put": 1,
        "tx_get": 8,
        "EN_tx_get": 1,
        "RDY_tx_get": 1,
        "request_get": 10,
        "EN_request_get": 1,
        "RDY_request_get": 1,
        "response_put": 10,
        "EN_response_put": 1,
        "RDY_response_put": 1,
        "requestPayloadLength": 8,
        "RDY_requestPayloadLength": 1,
        "responseMax": 8,
        "RDY_responseMax": 1,
        "responseReady": 1,
        "RDY_responseReady": 1,
    }
    assert {name: ports.get(name) for name in expected} == expected
