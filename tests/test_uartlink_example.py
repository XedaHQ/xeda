"""Check the UART link codecs, fault fence, and real host/RTL interoperability."""

from pathlib import Path

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import BscSim, Verilator

from .tool_utils import (
    require_bluesim,
    require_bsc,
    require_cocotb,
    require_iverilog,
    require_verilator,
    run_report,
)

ROOT = Path(__file__).resolve().parents[1] / "examples/bluespec/uart_link"


@pytest.mark.parametrize("simulator", ["bluesim", "iverilog", "verilator"])
@pytest.mark.parametrize(
    "bench",
    [
        "TbLinkCodecs",
        "TbUartLinkAdapter",
        "TbLinkDiagnostics",
        "TbReliableLinkAckValidation",
        "TbReliableLinkFullCacheAck",
        "TbReliableLinkControlLiveness",
    ],
)
def test_uartlink_streaming_components(bench, simulator, tmp_path, capfd):
    if simulator == "bluesim":
        require_bluesim()
    else:
        require_bsc()
        (require_iverilog if simulator == "iverilog" else require_verilator)()
    design = Design.from_file(ROOT / "uart_link.yaml")
    design.tb.sources = [ROOT / "tb" / f"{bench}.bsv"]
    design.tb.top = f"mk{bench}"
    flow = DefaultRunner(tmp_path, display_results=False).run_flow(
        BscSim, design, {"simulator": simulator, "promote_warnings": ["ALL"]}
    )
    captured = capfd.readouterr()
    assert flow is not None and flow.succeeded, run_report(flow, captured)
    assert "PASS" in captured.out


def test_uartlink_host_against_rtl(tmp_path, capfd):
    require_bsc()
    require_verilator()
    require_cocotb()
    design = Design(
        name="uart_link_cosim",
        design_root=ROOT,
        rtl={
            "sources": [
                "rtl/Cobs.bsv",
                "rtl/Crc32c.bsv",
                "rtl/UartLinkTypes.bsv",
                "rtl/ReliableLink.bsv",
                "tb/LinkPacketTest.bsv",
            ],
            "top": "mkLinkPacketTest",
            "clock": "CLK",
        },
        tb={"sources": ["tb/uart_link_cosim.py"], "cocotb": True},
        flows={"bsc": {"positive_reset": False, "promote_warnings": ["ALL"]}},
    )
    flow = DefaultRunner(tmp_path, display_results=False).run_flow(Verilator, design)
    captured = capfd.readouterr()
    assert flow is not None and flow.succeeded, run_report(flow, captured)
    assert flow.results["cocotb.tests"] == 1
    assert flow.results["cocotb.failures"] == 0
