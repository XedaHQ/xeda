"""Guards for tests that need a real EDA tool installed.

Most of the test suite runs against `tests/fake_tools/`, but a few flows are exercised
end-to-end against genuinely installed open-source tools. Those tests skip when the tool is
missing *or* installed-but-broken, so a checkout is testable on a machine without a full EDA
setup. Set ``XEDA_TESTS_REQUIRE_TOOLS=1`` (as CI should) to turn those skips into failures, so
a tool silently disappearing from CI is not mistaken for a passing run.

Probes run the tool the way the flow does rather than just asking for its version: a broken
backend (e.g. a ghdl whose LLVM shared library is missing) reports a version happily and then
fails on the first real invocation.
"""

import os
import shutil
import subprocess
import tempfile
from functools import lru_cache
from pathlib import Path
from typing import List, Optional, Sequence

import pytest

__all__ = [
    "fake_calls",
    "require_c_toolchain",
    "require_ghdl",
    "require_nextpnr_ecp5",
    "require_nvc",
    "require_verilator",
    "require_vivado",
    "require_yosys",
    "require_yosys_ghdl_plugin",
    "use_fake_tools",
    "checkout_work_dir",
    "require_docker",
    "require_docker_image",
]

REQUIRE_TOOLS = os.environ.get("XEDA_TESTS_REQUIRE_TOOLS", "").lower() in ("1", "true", "yes", "on")

_TRIVIAL_VHDL = "entity xeda_probe is end entity;\narchitecture rtl of xeda_probe is begin end;\n"
_TRIVIAL_C = "int xeda_probe(void) { return 0; }\n"
_TRIVIAL_VERILOG = (
    "module xeda_probe(input wire clk, output reg o);\n"
    "  always @(posedge clk) o <= ~o;\n"
    "endmodule\n"
)


def _command_succeeds(command: Sequence[str], cwd: Optional[str] = None) -> bool:
    if not shutil.which(command[0]):
        return False
    try:
        return (
            subprocess.run(list(command), capture_output=True, timeout=120, cwd=cwd).returncode == 0
        )
    except (OSError, subprocess.SubprocessError):
        return False


@lru_cache(maxsize=None)
def _probe(command: Sequence[str]) -> bool:
    return _command_succeeds(command)


@lru_cache(maxsize=None)
def _probe_ghdl() -> bool:
    """Analyze *and* elaborate, the two steps `GhdlSim` runs before a simulation.

    `ghdl --version` passes even when the code generator is unusable, and so does `analyze`:
    the front end parses VHDL without help from the backend, so a ghdl whose LLVM shared
    library is missing analyzes happily and only fails once elaboration asks it to generate
    code. The guarded tests elaborate and run, so the probe has to as well.
    """
    if not shutil.which("ghdl"):
        return False
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "xeda_probe.vhdl"
        src.write_text(_TRIVIAL_VHDL)
        if not _command_succeeds(["ghdl", "analyze", "--std=08", str(src)], cwd=tmp):
            return False
        return _command_succeeds(["ghdl", "elaborate", "--std=08", "xeda_probe"], cwd=tmp)


@lru_cache(maxsize=None)
def _probe_yosys_ghdl_plugin() -> bool:
    """Actually read VHDL through the plugin, rather than only loading it.

    `plugin -i ghdl` succeeds whenever the shared object is present, but the plugin still needs a
    usable GHDL installation behind it -- oss-cad-suite's copy fails with `cannot find "std"
    library` unless its `environment` script has been sourced to set `GHDL_PREFIX`. Loading alone
    would let a test run and fail where it should skip.
    """
    if not shutil.which("yosys"):
        return False
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "xeda_probe.vhdl"
        src.write_text(_TRIVIAL_VHDL)
        return _command_succeeds(
            ["yosys", "-p", f"plugin -i ghdl; ghdl --std=08 {src} -e xeda_probe"], cwd=tmp
        )


def _require(what: str, ok: bool, how: str) -> None:
    if ok:
        return
    msg = f"{what} is unavailable or not functional ({how} failed)"
    if REQUIRE_TOOLS:
        pytest.fail(f"{msg}; XEDA_TESTS_REQUIRE_TOOLS is set")
    pytest.skip(msg)


def _require_command(what: str, command: List[str]) -> None:
    _require(what, _probe(tuple(command)), f"`{' '.join(command)}`")


@lru_cache(maxsize=None)
def _probe_c_toolchain() -> bool:
    """Compile a trivial C file, as cocotb and Verilator do when building a model.

    On macOS the compiler is present but refuses to run until the Xcode license is accepted,
    so `shutil.which` is not enough to tell whether it works.
    """
    compiler = os.environ.get("CC") or shutil.which("cc") or shutil.which("clang")
    if not compiler:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "xeda_probe.c"
        src.write_text(_TRIVIAL_C)
        return _command_succeeds(
            [compiler, "-c", str(src), "-o", str(Path(tmp) / "xeda_probe.o")], cwd=tmp
        )


def require_c_toolchain() -> None:
    """A working C compiler, needed by cocotb's C reference models and Verilator's models."""
    _require("a C toolchain", _probe_c_toolchain(), "compiling a trivial C file")


def require_ghdl() -> None:
    _require("ghdl", _probe_ghdl(), "`ghdl analyze` + `elaborate` of a trivial entity")


@lru_cache(maxsize=None)
def _probe_nvc() -> bool:
    """Analyze *and* elaborate, as the flow does.

    `nvc --version` answers from the binary alone. Elaboration is what exercises the code
    generator and the bundled standard libraries, which is where a half-installed nvc fails.
    """
    if not shutil.which("nvc"):
        return False
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "xeda_probe.vhdl"
        src.write_text(_TRIVIAL_VHDL)
        return _command_succeeds(["nvc", "--std=08", "-a", str(src), "-e", "xeda_probe"], cwd=tmp)


def require_nvc() -> None:
    _require("nvc", _probe_nvc(), "`nvc -a` + `-e` of a trivial entity")
    # the Trivium example drives a C reference model through cocotb
    require_c_toolchain()


@lru_cache(maxsize=None)
def _probe_verilator() -> bool:
    """Verilate *and* build, as the flow does.

    The flow's real work is `--cc ... --build`: Verilator emits C++ and then compiles it against
    its own runtime headers. A binary whose installation prefix no longer matches its headers
    reports a version cheerfully and fails only once it builds, so `--version` is not evidence.
    """
    if not shutil.which("verilator"):
        return False
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "xeda_probe.v"
        src.write_text(_TRIVIAL_VERILOG)
        return _command_succeeds(
            ["verilator", "--cc", "--build", "--Mdir", "obj_probe", str(src)], cwd=tmp
        )


def require_verilator() -> None:
    # Checked first: the C toolchain is what `--build` needs, and a missing compiler should be
    # reported as such rather than as a broken Verilator.
    require_c_toolchain()
    _require("verilator", _probe_verilator(), "`verilator --cc --build` of a trivial module")


def require_yosys() -> None:
    _require_command("yosys", ["yosys", "-V"])


def require_nextpnr_ecp5() -> None:
    """nextpnr-ecp5 plus the yosys that synthesizes for it."""
    require_yosys()
    _require_command("nextpnr-ecp5", ["nextpnr-ecp5", "--version"])


def require_yosys_ghdl_plugin() -> None:
    """yosys plus a working ghdl plugin, needed to synthesize VHDL sources through yosys."""
    require_yosys()
    _require(
        "the yosys ghdl plugin",
        _probe_yosys_ghdl_plugin(),
        "reading a trivial VHDL entity through `yosys -p 'plugin -i ghdl; ghdl ...'`",
    )


# ---------------------------------------------------------------------------------------------
# Fake tools: `tests/fake_tools/` stands in for the proprietary tools. Each runs the TCL script it
# is handed under tclsh with the tool's commands recorded, as `fake_<tool>.calls` in the run
# directory -- so a script the real tool would reject fails the fake as well.
# ---------------------------------------------------------------------------------------------

FAKE_TOOLS_DIR = Path(__file__).parent / "fake_tools"


def use_fake_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the fake tools first on PATH for the rest of the test."""
    monkeypatch.setenv("PATH", str(FAKE_TOOLS_DIR) + os.pathsep + os.environ.get("PATH", ""))


def fake_calls(run_dir: Path, elements: bool = False) -> List[List[str]]:
    """Every tool command the fake tools recorded under `run_dir`, in the order each tool ran
    them, as its arguments -- and, with `elements`, also the items of each argument that is a TCL
    list, such as the files of `[list "a b.v" c.v]` (an argument with a space is a list too, so
    only a presence check wants them). A TCL error a fake recorded fails the test."""
    calls: List[List[str]] = []
    for log in sorted(run_dir.rglob("fake_*.calls")):
        for line in log.read_text().splitlines():
            kind, _, value = line.partition(" ")
            if kind == "TCL-ERROR":
                pytest.fail(f"{log.name}: the script failed: {value}")
            elif kind == "CALL":
                calls.append([])
            elif calls and (kind == "ARG" or (elements and kind == "ELEM")):
                calls[-1].append(value)
    return calls


# ---------------------------------------------------------------------------------------------
# Opt-in layers, skipped unless their variable is set -- and then insisting on what they need:
#   XEDA_TESTS_VIVADO=1  flows run by a real `vivado` (tests/test_vivado_real.py)
#   XEDA_TESTS_DOCKER=1  flows run `dockerized`, in their default images (tests/test_dockerized.py)
# ---------------------------------------------------------------------------------------------


def _opted_in(variable: str) -> bool:
    """Check whether an optional tool test was requested."""
    return os.environ.get(variable, "").lower() in ("1", "true", "yes", "on")


def require_vivado() -> None:
    """Run Vivado only when asked (`XEDA_TESTS_VIVADO=1`), and then insist on it."""
    if not _opted_in("XEDA_TESTS_VIVADO"):
        pytest.skip("set XEDA_TESTS_VIVADO=1 to run the tests that run Vivado")
    if shutil.which("vivado") is None:
        pytest.fail("XEDA_TESTS_VIVADO is set, but no `vivado` is on PATH")


@lru_cache(maxsize=None)
def _docker_works() -> bool:
    """Check whether Docker is usable for opt-in tests."""
    return _command_succeeds(["docker", "info"])


def require_docker() -> None:
    """Run tools in containers only when asked (`XEDA_TESTS_DOCKER=1`), then insist on a working
    `docker`."""
    if not _opted_in("XEDA_TESTS_DOCKER"):
        pytest.skip("set XEDA_TESTS_DOCKER=1 to run the tests that run tools in containers")
    if not _docker_works():
        pytest.fail("XEDA_TESTS_DOCKER is set, but `docker info` fails")


def require_docker_image(image: str) -> None:
    """`require_docker`, and `image` present locally: a missing one skips the test with the pull
    to run, since images of commercial tools are tens of GB and a test never pulls one."""
    require_docker()
    if not _command_succeeds(["docker", "image", "inspect", image]):
        pytest.skip(f"{image} is not present: `docker pull {image}` to run this test")


def checkout_work_dir(prefix: str) -> Path:
    """A fresh directory for a test that runs a tool elsewhere -- in a container, or through a
    `vivado` wrapper that runs one: under `XEDA_TESTS_WORK_DIR` if set, else in the checkout's
    `xeda_run/`, which such a container can mount where the system temp directory is not."""
    base = Path(os.environ.get("XEDA_TESTS_WORK_DIR") or Path(__file__).parent.parent / "xeda_run")
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=base))
