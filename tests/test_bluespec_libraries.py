"""The Bluespec source libraries keep defaults bench-free and expose runnable test targets."""

from __future__ import annotations

from pathlib import Path

import pytest

from xeda import Design

EXAMPLES = Path(__file__).resolve().parents[1] / "examples/bluespec"

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
