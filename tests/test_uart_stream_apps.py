"""Run the shipped streaming clients through the real application RTL and UART pins."""

from pathlib import Path

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import Verilator

from .design_edits import with_updates
from .tool_utils import require_bsc, require_cocotb, require_verilator, run_report

APPS = Path(__file__).resolve().parents[1] / "examples/bluespec/uart_link/apps"


@pytest.mark.parametrize("application", ["gcd", "ascon", "ascon128a"])
@pytest.mark.parametrize("serial", [False, True], ids=["bytes", "uart-pins"])
def test_uart_stream_application(application, serial, tmp_path, capfd, monkeypatch):
    """The actual host client checks core results across complete reliable-link exchanges."""
    require_bsc()
    require_verilator()
    require_cocotb()
    name = "ascon" if application.startswith("ascon") else "gcd"
    path = APPS / f"{name}.yaml"
    assert path.is_file(), f"missing UART application design: {path}"
    design = Design.load(path, target="sim_uart" if serial else "sim_bytes")
    if application == "ascon128a":
        design = with_updates(design, rtl={"defines": {**design.rtl.defines, "ASCON128A": True}})
    monkeypatch.setenv("STREAM_SOURCE_ROOT", str(APPS.parents[3]))
    monkeypatch.setenv("STREAM_APP", application)
    monkeypatch.setenv("STREAM_SERIAL", str(int(serial)))
    flow = DefaultRunner(tmp_path / "run", display_results=False).run_flow(
        Verilator, design, {"timeout": 180}
    )
    captured = capfd.readouterr()
    assert flow is not None and flow.succeeded, run_report(flow, captured)
    assert flow.results["cocotb.tests"] >= 1
    assert flow.results["cocotb.failures"] == 0
    assert flow.results["cocotb.errors"] == 0
    assert flow.results["cocotb.skipped"] == 0


@pytest.mark.parametrize(
    "variant_a,unroll",
    [(False, 1), (False, 2), (True, 1), (True, 4)],
    ids=["ascon128-u1", "ascon128-u2", "ascon128a-u1", "ascon128a-u4"],
)
def test_ascon_stream_word_wrapper(variant_a, unroll, tmp_path):
    """Independent vectors and malformed-word recovery exercise the raw stream boundary."""
    import subprocess

    from .tool_utils import require_bluesim

    require_bsc()
    require_bluesim()
    root = APPS.parents[3]
    ascon = root / "examples/bluespec/ascon"
    common = [
        "bsc",
        "-sim",
        "-D",
        f"UNROLL_FACTOR={unroll}",
        "-p",
        f"+:{ascon / 'rtl'}:{ascon / 'tb'}:{APPS.parent / 'rtl'}",
        "-bdir",
        str(tmp_path),
        "-simdir",
        str(tmp_path),
        "-info-dir",
        str(tmp_path),
    ]
    if variant_a:
        common += ["-D", "ASCON128A"]
    for name, marker in [
        ("Backpressure", "PASS: failed-state input drains"),
        ("Validation", "PASS: Ascon40 malformed START drains"),
        ("Vectors", "PASS: 61 Ascon stream reference cases"),
    ]:
        module = f"mkTbAsconStream{name}40" if name == "Validation" else f"mkTbAsconStream{name}"
        binary = tmp_path / f"run_{name}"
        commands = [
            common + ["-u", "-g", module, str(ascon / "tb" / f"TbAsconStream{name}.bsv")],
            common + ["-Xc++", "-O0", "-e", module, "-o", str(binary)],
            [str(binary)],
        ]
        for step, command in enumerate(commands):
            result = subprocess.run(command, capture_output=True, text=True, timeout=180)
            output = result.stdout + result.stderr
            (tmp_path / f"{name}-{step}.log").write_text(output)
            assert result.returncode == 0, output
        assert marker in output, output
        assert "FAIL:" not in output, output
