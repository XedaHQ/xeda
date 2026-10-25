"""The Bluespec source libraries keep defaults bench-free and expose runnable test targets."""

from __future__ import annotations

from pathlib import Path

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import Bsc, BscSim

from .tool_utils import require_bluesim, require_bsc, run_report

EXAMPLES = Path(__file__).resolve().parents[1] / "examples/bluespec"

LIBRARY_SIMULATIONS = [
    ("uart/uart-library.yaml", "sim", "tb", "mkTbUart"),
    ("uart_link/leanlink-library.yaml", "test", "tb", "mkTbLeanLink"),
    ("uart_link/stream-exchange-library.yaml", "test", "rtl", "mkTbStreamExchange"),
    ("uart_link/stream-exchange-library.yaml", "gcd", "rtl", "mkTbGcdStream"),
    ("uart_link/stream-exchange-library.yaml", "reset_interrupt", "rtl", "mkTbResetInterrupt"),
    (
        "uart_link/stream-exchange-library.yaml",
        "output_replacement",
        "rtl",
        "mkTbStreamExchangeOutputReplacement",
    ),
    (
        "uart_link/stream-exchange-library.yaml",
        "word5_lengths",
        "rtl",
        "mkTbStreamExchangeWord5Lengths",
    ),
    (
        "uart_link/stream-exchange-library.yaml",
        "width8_page4",
        "rtl",
        "mkTbStreamExchangeByteAlignedPage4",
    ),
    ("uart_link/stream-exchange-library.yaml", "page8_cap", "rtl", "mkTbStreamExchangePage8Cap"),
    ("uart_link/stream-exchange-library.yaml", "page4_w12", "rtl", "mkTbStreamExchangePage4W12"),
    *[
        (
            "uart_link/stream-exchange-library.yaml",
            f"width{width}",
            "rtl",
            f"mkTbStreamExchangeW{width}",
        )
        for width in (1, 2, 3, 4, 5, 6, 7, 9, 13)
    ],
    ("gcd/gcd-library.yaml", "unit", "tb", "mkTbGcdUnit"),
    ("gcd/gcd-stream-library.yaml", "stream", "rtl", "mkTbGcdStream"),
    ("ascon/ascon-library.yaml", "core", "tb", "mkTbAscon"),
    ("ascon/ascon-stream-library.yaml", "stream_vectors", "tb", "mkTbAsconStreamVectors"),
    ("ascon/ascon-stream-library.yaml", "stream_validation", "tb", "mkTbAsconStreamValidation40"),
    ("ascon/ascon-stream-library.yaml", "stream_backpressure", "tb", "mkTbAsconStreamBackpressure"),
]

LIBRARIES = [
    (
        "uart/uart-library.yaml",
        ["RingFifo.bsv", "UartTypes.bsv", "BitTimer.bsv", "UartRx.bsv", "UartTx.bsv", "Uart.bsv"],
        "sim",
        "mkTbUart",
        ["TbUartModels.bsv", "TbUart.bsv"],
    ),
    (
        "uart_link/leanlink-library.yaml",
        ["LeanLink.bsv"],
        "test",
        "mkTbLeanLink",
        ["LeanLinkFrames.bsv", "TbLeanLink.bsv"],
    ),
    (
        "uart_link/stream-exchange-library.yaml",
        ["LeanLink.bsv", "StreamExchange.bsv"],
        "test",
        "mkTbStreamExchange",
        ["TbStreamExchange.bsv"],
    ),
    (
        "gcd/gcd-library.yaml",
        ["GcdEngine.bsv", "GcdUnit.bsv"],
        "unit",
        "mkTbGcdUnit",
        ["TbGcdUnit.bsv"],
    ),
    (
        "gcd/gcd-stream-library.yaml",
        ["LeanLink.bsv", "StreamExchange.bsv", "GcdEngine.bsv", "GcdUnit.bsv", "GcdStream.bsv"],
        "stream",
        "mkTbGcdStream",
        ["TbStreamExchange.bsv"],
    ),
    (
        "ascon/ascon-library.yaml",
        ["AsconTypes.bsv", "AsconRound.bsv", "AsconCipher.bsv", "Ascon.bsv"],
        "core",
        "mkTbAscon",
        ["AsconVectors.bsv", "TbAscon.bsv"],
    ),
    (
        "ascon/ascon-stream-library.yaml",
        [
            "LeanLink.bsv",
            "StreamExchange.bsv",
            "AsconTypes.bsv",
            "AsconRound.bsv",
            "AsconCipher.bsv",
            "Ascon.bsv",
            "AsconStream.bsv",
        ],
        "stream_vectors",
        "mkTbAsconStreamVectors",
        ["AsconVectors.bsv", "TbAsconStreamVectors.bsv"],
    ),
]


@pytest.mark.parametrize("path,rtl_sources,target,top,tb_sources", LIBRARIES)
def test_library_default_is_bench_free(path, rtl_sources, target, top, tb_sources):
    design = Design.from_file(EXAMPLES / path)

    assert [source.file.name for source in design.rtl.sources] == rtl_sources
    assert design.rtl.top is None
    assert not design.tb.sources
    assert not design.tb.top


@pytest.mark.parametrize("path,rtl_sources,target,top,tb_sources", LIBRARIES)
def test_library_test_target_selects_existing_sources(path, rtl_sources, target, top, tb_sources):
    design = Design.from_file(EXAMPLES / path, target=target)

    assert design.rtl.sources
    assert list(design.tb.top) == [top] or design.rtl.top == top
    source_names = [source.file.name for source in (*design.rtl.sources, *design.tb.sources)]
    assert all(name in source_names for name in tb_sources)
    assert not any(source.file.name.endswith("-library.yaml") for source in design.tb.sources)


@pytest.mark.parametrize(
    "target,top",
    [
        ("gcd", "mkTbGcdStream"),
        ("reset_interrupt", "mkTbResetInterrupt"),
        ("output_replacement", "mkTbStreamExchangeOutputReplacement"),
        ("word5_lengths", "mkTbStreamExchangeWord5Lengths"),
        ("width1", "mkTbStreamExchangeW1"),
        ("width2", "mkTbStreamExchangeW2"),
        ("width3", "mkTbStreamExchangeW3"),
        ("width4", "mkTbStreamExchangeW4"),
        ("width5", "mkTbStreamExchangeW5"),
        ("width6", "mkTbStreamExchangeW6"),
        ("width7", "mkTbStreamExchangeW7"),
        ("width9", "mkTbStreamExchangeW9"),
        ("width13", "mkTbStreamExchangeW13"),
    ],
)
def test_stream_exchange_library_keeps_width_targets(target, top):
    design = Design.from_file(EXAMPLES / "uart_link/stream-exchange-library.yaml", target=target)

    assert design.rtl.top == top


@pytest.mark.real_tool
@pytest.mark.parametrize("path,target,top_field,expected_top", LIBRARY_SIMULATIONS)
def test_every_library_test_target_runs(path, target, top_field, expected_top, tmp_path, capfd):
    """Run every library test target in CI so source and testbench changes stay executable."""
    require_bluesim()
    design = Design.from_file(EXAMPLES / path, target=target)
    actual_top = getattr(design, top_field).top
    if top_field == "rtl":
        assert actual_top == expected_top
    else:
        assert list(actual_top) == [expected_top]

    flow = DefaultRunner(tmp_path / target, display_results=False).run_flow(
        BscSim, design, {"simulator": "bluesim"}
    )
    captured = capfd.readouterr()
    assert flow is not None and flow.succeeded, run_report(flow, captured)
    assert "PASS" in captured.out


@pytest.mark.real_tool
@pytest.mark.parametrize(
    "defines",
    [
        {"LL_LOGW": 2, "LL_LOGP": 4, "LL_APP_GCD": True},
        {"LL_LOGW": 2, "LL_LOGP": 4},
    ],
    ids=("configured-gcd", "default-echo"),
)
def test_stream_app_bytes_uses_link_configuration(defines, tmp_path, capfd):
    """Elaborate both app-specific and default byte tops with the board's link macros."""
    require_bsc()
    design = Design.from_file(EXAMPLES / "uart_link/apps/gcd.yaml", target="sim_bytes")
    app_defines = {
        name: value
        for name, value in design.rtl.defines.items()
        if name not in {"LL_APP_GCD", "LL_APP_ASCON"}
    }
    design.rtl.defines = {**app_defines, **defines}
    flow = DefaultRunner(tmp_path, display_results=False).run_flow(Bsc, design)
    captured = capfd.readouterr()
    assert flow is not None and flow.succeeded, run_report(flow, captured)
