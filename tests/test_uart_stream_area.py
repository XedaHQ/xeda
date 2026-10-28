"""Measure the complete UART transport tops against the logic LUT gate."""

from __future__ import annotations

import re
from pathlib import Path
from types import SimpleNamespace

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
BLOCK_RAM_CELLS = (
    "RAMB18",
    "RAMB18E1",
    "RAMB18E2",
    "RAMB36",
    "RAMB36E1",
    "RAMB36E2",
)


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
    assert results["lut_use.ram"] > 0
    block_rams = sum(utilization.get(cell, 0) for cell in BLOCK_RAM_CELLS)
    if target.endswith("_w8"):
        assert block_rams > 0
        assert utilization.get("RAM64M", 0) == 0
    else:
        assert block_rams == 0
        assert utilization.get("RAM64M", 0) > 0


@pytest.mark.parametrize("cell", BLOCK_RAM_CELLS)
def test_memory_mapping_recognizes_block_ram_variants(cell):
    utilization = {cell: 1}
    block_ram_flow = SimpleNamespace(results={"_utilization": utilization, "lut_use.ram": 1})
    distributed_flow = SimpleNamespace(
        results={"_utilization": {**utilization, "RAM64M": 1}, "lut_use.ram": 1}
    )

    _assert_memory_mapping(block_ram_flow, "gcd_w8")
    with pytest.raises(AssertionError):
        _assert_memory_mapping(distributed_flow, "gcd_w1")


@pytest.mark.parametrize("target,input_width,output_width", ADAPTERS)
def test_uart_stream_adapter_area(target, input_width, output_width, tmp_path, capfd):
    """Keep both stream word widths and the full reset-aware adapter below 500 logic LUTs."""
    require_bsc()
    require_yosys()
    design = Design.load(AREA, target=target)
    run_root = tmp_path / target
    flow = DefaultRunner(run_root, display_results=False).run_flow(YosysFpga, design)
    captured = capfd.readouterr()
    assert flow is not None and flow.succeeded, run_report(flow, captured)
    assert flow.results["lut_basis.stage"] == "mapped"
    assert flow.results["lut_basis.method"] == "primitive-footprint estimate"
    assert 0 <= flow.results["lut_use.logic"] <= 500
    assert flow.results["lut_use.ram"] >= 0
    assert flow.results["lut_use.srl"] >= 0
    assert flow.results["ff"] >= 0
    assert flow.results["cell_types"]["CARRY4"] >= 0
    assert flow.results["lut"] == (
        flow.results["lut_use.logic"] + flow.results["lut_use.ram"] + flow.results["lut_use.srl"]
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
    design = Design.load(AREA, target=target)
    run_root = tmp_path / target
    flow = DefaultRunner(run_root, display_results=False).run_flow(YosysFpga, design)
    captured = capfd.readouterr()
    assert flow is not None and flow.succeeded, run_report(flow, captured)
    assert flow.results["lut_basis.stage"] == "mapped"
    assert flow.results["lut_basis.method"] == "primitive-footprint estimate"
    assert flow.results["lut_use.logic"] >= 0
    assert flow.results["lut_use.ram"] >= 0
    assert flow.results["lut_use.srl"] >= 0
    assert flow.results["ff"] >= 0
    assert flow.results["cell_types"]["CARRY4"] >= 0
    assert flow.results["lut"] == (
        flow.results["lut_use.logic"] + flow.results["lut_use.ram"] + flow.results["lut_use.srl"]
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
