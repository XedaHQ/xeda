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
    "require_c_toolchain",
    "require_ghdl",
    "require_nextpnr_ecp5",
    "require_nvc",
    "require_verilator",
    "require_yosys",
    "require_yosys_ghdl_plugin",
]

REQUIRE_TOOLS = os.environ.get("XEDA_TESTS_REQUIRE_TOOLS", "").lower() in ("1", "true", "yes", "on")

_TRIVIAL_VHDL = "entity xeda_probe is end entity;\narchitecture rtl of xeda_probe is begin end;\n"
_TRIVIAL_C = "int xeda_probe(void) { return 0; }\n"


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
    """`ghdl --version` passes even when the code generator is unusable, so actually analyze."""
    if not shutil.which("ghdl"):
        return False
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "xeda_probe.vhdl"
        src.write_text(_TRIVIAL_VHDL)
        return _command_succeeds(["ghdl", "analyze", "--std=08", str(src)], cwd=tmp)


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
    _require("ghdl", _probe_ghdl(), "`ghdl analyze` of a trivial entity")


def require_nvc() -> None:
    _require_command("nvc", ["nvc", "--version"])
    # the Trivium example drives a C reference model through cocotb
    require_c_toolchain()


def require_verilator() -> None:
    _require_command("verilator", ["verilator", "--version"])
    # Verilator compiles the design to C++ and builds a native model
    require_c_toolchain()


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
