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

import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import Any, List, NamedTuple, Optional, Sequence, Tuple

import pytest

__all__ = [
    "HANG_GUARD",
    "OPENROAD_COMMANDS",
    "fake_calls",
    "fake_returns",
    "require_bluesim",
    "require_bluesim_evidence",
    "require_bsc",
    "require_c_toolchain",
    "require_cxx_toolchain",
    "require_cxxrtl_evidence",
    "require_cocotb",
    "require_ghdl",
    "require_git",
    "require_iverilog",
    "require_nextpnr_ecp5",
    "require_nvc",
    "require_nvc_evidence",
    "require_openroad",
    "require_tclsh",
    "require_verilator",
    "require_vivado",
    "require_yosys",
    "require_yosys_config",
    "require_yosys_ghdl_plugin",
    "require_yosys_tcl",
    "use_fake_asic_tools",
    "use_fake_openroad",
    "use_fake_tools",
    "yosys_json_attribute_holders",
    "checkout_work_dir",
    "require_docker",
    "require_docker_image",
    "require_modelsim",
    "check_after_the_racy_window",
    "producers_of",
    "run_outputs_state",
    "run_report",
]


#: The timeout, in seconds, of a subprocess that a test runs without testing the timeout. It only
#: stops a hang. A child that is CPU-bound (it imports Xeda, or builds a design) is several times
#: slower on a busy runner, and a shorter limit would fail the test for the load alone.
HANG_GUARD = 300


def run_report(flow: Any, captured: Any, tail: int = 4000) -> str:
    """Return the text for the message of an assertion that a run succeeded: the error that the
    flow recorded, then the tail of what the run printed to stderr and to stdout.

    `captured` is the result of `capfd.readouterr()`, read once. A tool's diagnostic may be on
    either stream, and a parallel build buries the first error far from the end, so the flow's
    own error comes first."""
    error = (getattr(flow, "results", None) or {}).get("error") or {}
    lines = []
    if error:
        lines.append(f"flow error: {error.get('type')}: {error.get('message')}")
    lines.append(f"--- stderr (last {tail} characters) ---\n{captured.err[-tail:]}")
    lines.append(f"--- stdout (last {tail} characters) ---\n{captured.out[-tail:]}")
    return "\n".join(lines)


REQUIRE_TOOLS = os.environ.get("XEDA_TESTS_REQUIRE_TOOLS", "").lower() in ("1", "true", "yes", "on")

_TRIVIAL_VHDL = "entity xeda_probe is end entity;\narchitecture rtl of xeda_probe is begin end;\n"
_TRIVIAL_C = "int xeda_probe(void) { return 0; }\n"
_TRIVIAL_VERILOG = (
    "module xeda_probe(input wire clk, output reg o);\n"
    "  always @(posedge clk) o <= ~o;\n"
    "endmodule\n"
)
_TRIVIAL_VERILOG_TB = "module xeda_probe;\n  initial begin\n    $finish;\n  end\nendmodule\n"
_TRIVIAL_BSV = (
    "package XedaProbe;\n"
    "(* synthesize *)\n"
    "module mkXedaProbe(Empty);\n"
    "endmodule\n"
    "endpackage\n"
)
_TRIVIAL_BSV_SIM = (
    "package XedaProbe;\n"
    "(* synthesize *)\n"
    "module mkXedaProbe(Empty);\n"
    "  rule done;\n"
    "    $finish(0);\n"
    "  endrule\n"
    "endmodule\n"
    "endpackage\n"
)


def _command_succeeds(
    command: Sequence[str], cwd: Optional[str] = None, timeout: int = 120
) -> bool:
    if not shutil.which(command[0]):
        return False
    try:
        return (
            subprocess.run(list(command), capture_output=True, timeout=timeout, cwd=cwd).returncode
            == 0
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


@lru_cache(maxsize=None)
def _probe_cxx_toolchain() -> bool:
    """Compile and execute a native C++ helper, as simulator evidence monitors require."""
    compiler = os.environ.get("CXX") or shutil.which("c++")
    if not compiler:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        source = Path(tmp) / "probe.cpp"
        binary = Path(tmp) / "probe"
        source.write_text("int main() { return 0; }\n")
        return _command_succeeds(
            [compiler, str(source), "-o", str(binary)], cwd=tmp
        ) and _command_succeeds([str(binary)], cwd=tmp)


def require_cxx_toolchain() -> None:
    """A working native C++ compiler and runtime for simulation evidence helpers."""
    _require("a C++ toolchain", _probe_cxx_toolchain(), "compiling and executing a C++ helper")


def require_cocotb() -> None:
    """The cocotb Python package and configuration executable used by simulator flows."""
    _require_command("cocotb", [sys.executable, "-c", "import cocotb"])
    _require_command("cocotb-config", ["cocotb-config", "--version"])


def require_ghdl() -> None:
    _require("ghdl", _probe_ghdl(), "`ghdl analyze` + `elaborate` of a trivial entity")


def require_git() -> None:
    """The `git` program, which GitPython runs to clone a repository."""
    _require_command("git", ["git", "--version"])


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
def _probe_nvc_evidence() -> bool:
    """Build/load the shipped passive monitor and distinguish equal-time drain/cutoff."""
    from importlib.resources import files

    executable = shutil.which("nvc")
    compiler = shutil.which("c++")
    if executable is None or compiler is None:
        return False
    prefix = Path(executable).resolve().parent.parent
    flags = (
        ["-dynamiclib", "-undefined", "dynamic_lookup"]
        if sys.platform == "darwin"
        else ["-shared", "-fPIC"]
    )
    with tempfile.TemporaryDirectory(prefix="xeda-nvc-evidence-") as tmp:
        root = Path(tmp)
        (root / "nvc_end.cpp").write_text(
            files("xeda.flows.nvc").joinpath("templates/nvc_end.cpp").read_text()
        )
        (root / "sim_record.h").write_text(
            files("xeda.flow").joinpath("templates/sim_record.h").read_text()
        )
        if not _command_succeeds(
            [compiler, *flags, "-I", str(prefix / "include"), "nvc_end.cpp", "-o", "nvc_end.so"],
            cwd=tmp,
        ):
            return False
        for clock, body, time, pending in (
            ("", "wait;", 0, None),
            ("", "wait for 5 ns; wait;", 5_000_000, None),
            ("clk <= not clk after 1 ns;", "wait;", 5_000_000, 6_000_000),
        ):
            (root / "tb.vhdl").write_text(
                "entity tb is end; architecture rtl of tb is signal clk: bit := '0'; begin "
                + clock
                + " process begin "
                + body
                + " end process; end;"
            )
            command = [
                executable,
                "--std=08",
                "-a",
                "tb.vhdl",
                "-e",
                "tb",
                "-r",
                "--load=./nvc_end.so",
                "--stop-time=5ns",
            ]
            try:
                result = subprocess.run(
                    command,
                    cwd=tmp,
                    capture_output=True,
                    timeout=10,
                    env={**os.environ, "XEDA_NVC_END_RECORD": "nvc_end.json"},
                )
                record = json.loads((root / "nvc_end.json").read_text())
            except (OSError, ValueError, subprocess.SubprocessError):
                return False
            if (
                result.returncode != 0
                or b"XEDA_NVC_RUNTIME_START\n" not in result.stderr
                or record != {"time": time, "time_unit": "1fs", "next_time": pending}
            ):
                return False
        return True


def require_nvc_evidence() -> None:
    """NVC plus a working C++/VHPI passive end and pending-activity monitor."""
    require_nvc()
    require_cxx_toolchain()
    _require(
        "nvc simulation evidence",
        _probe_nvc_evidence(),
        "building/loading vhpi_user.h monitor and observing drain/cutoff",
    )


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


@lru_cache(maxsize=None)
def _probe_tclsh() -> bool:
    """Run a trivial script: `tclsh` with no argument would wait for a terminal."""
    if not shutil.which("tclsh"):
        return False
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "xeda_probe.tcl"
        script.write_text("puts ok\n")
        return _command_succeeds(["tclsh", str(script)], cwd=tmp)


def require_tclsh() -> None:
    """`tclsh`, which runs a rendered TCL script against stub commands where no tool is installed
    that could run it (yosys builds without TCL support, for one)."""
    _require("tclsh", _probe_tclsh(), "`tclsh` running a trivial script")


def require_yosys() -> None:
    _require_command("yosys", ["yosys", "-V"])


def require_klayout() -> None:
    _require_command("klayout", ["klayout", "-v"])


def require_yosys_config() -> None:
    """The `yosys-config` executable, which a yosys install need not ship.

    It is a separate program beside `yosys` (`CxxRtl` asks it for its include directory, and the
    LUT-footprint sweep for the cell library's location), so it is a probe of its own rather than
    part of `require_yosys()`: a test that needs only `yosys` must not skip for want of it.
    """
    _require_command("yosys-config", ["yosys-config", "--datdir"])


@lru_cache(maxsize=None)
def _probe_cxxrtl_evidence() -> bool:
    """Generate/link a real RTL assertion and execute the shipped monitor at both thresholds."""
    import xeda

    package = Path(xeda.__file__).parent
    with tempfile.TemporaryDirectory() as tmp:
        work = Path(tmp)
        for relative in (
            "flow/templates/sim_record.h",
            "flows/yosys/templates/cxxrtl_evidence.h",
            "flows/yosys/templates/cxxrtl_evidence.cpp",
        ):
            shutil.copyfile(package / relative, work / Path(relative).name)
        (work / "dut.sv").write_text("module dut(input a); always @* assert(a); endmodule\n")
        (work / "main.cpp").write_text(
            '#include "model.h"\nint main() { cxxrtl_design::p_dut top; '
            "top.p_a.set<bool>(false); top.step(); return 0; }\n"
        )
        if not _command_succeeds(
            [
                "yosys",
                "-Q",
                "-T",
                "-p",
                "read_verilog -formal -sv dut.sv; hierarchy -top dut; proc; "
                "write_cxxrtl -header model.cpp",
            ],
            cwd=tmp,
        ):
            return False
        try:
            query = subprocess.run(
                ["yosys-config", "--datdir/include"], capture_output=True, text=True, timeout=30
            )
            if query.returncode or not query.stdout.strip():
                return False
            include = Path(query.stdout.strip())
            if not _command_succeeds(
                [
                    "g++",
                    "-std=c++14",
                    "-DNDEBUG",
                    "-DCXXRTL_NDEBUG",
                    "-I.",
                    f"-I{include}",
                    f"-I{include / 'backends/cxxrtl/runtime'}",
                    "-include",
                    "cxxrtl_evidence.h",
                    "model.cpp",
                    "main.cpp",
                    "cxxrtl_evidence.cpp",
                    "-o",
                    "sim",
                ],
                cwd=tmp,
            ):
                return False
            for severity, expected in (("error", 1), ("failure", 0)):
                record, events = work / "end.json", work / "events.jsonl"
                record.unlink(missing_ok=True)
                events.unlink(missing_ok=True)
                result = subprocess.run(
                    [str(work / "sim")],
                    cwd=tmp,
                    capture_output=True,
                    timeout=10,
                    env={
                        **os.environ,
                        "XEDA_CXXRTL_END_RECORD": str(record),
                        "XEDA_CXXRTL_EVENTS": str(events),
                        "XEDA_CXXRTL_FAIL_SEVERITY": severity,
                    },
                )
                if result.returncode != expected:
                    return False
                if json.loads(record.read_text())["ended_by"] != "exit":
                    return False
                if (
                    not all(
                        json.loads(line)["kind"] == "error"
                        for line in events.read_text().splitlines()
                    )
                    or not events.stat().st_size
                ):
                    return False
            return True
        except (OSError, ValueError, KeyError, subprocess.SubprocessError):
            return False


def require_cxxrtl_evidence() -> None:
    """Yosys/C++ plus a working generated RTL-check and driver-execution monitor."""
    require_yosys()
    require_c_toolchain()
    require_cxx_toolchain()
    _require(
        "CXXRTL evidence", _probe_cxxrtl_evidence(), "linking/running the shipped RTL-check monitor"
    )


def require_nextpnr_ecp5() -> None:
    """nextpnr-ecp5 plus the yosys that synthesizes for it."""
    require_yosys()
    _require_command("nextpnr-ecp5", ["nextpnr-ecp5", "--version"])


def require_nextpnr_ice40() -> None:
    """nextpnr-ice40 plus the yosys that synthesizes for it."""
    require_yosys()
    _require_command("nextpnr-ice40", ["nextpnr-ice40", "--version"])


def require_yosys_ghdl_plugin() -> None:
    """yosys plus a working ghdl plugin, needed to synthesize VHDL sources through yosys."""
    require_yosys()
    _require(
        "the yosys ghdl plugin",
        _probe_yosys_ghdl_plugin(),
        "reading a trivial VHDL entity through `yosys -p 'plugin -i ghdl; ghdl ...'`",
    )


# bsc/bsc_sim need features only present from this release; an older bsc runs happily but is not
# functional for xeda's purposes.
_MIN_BSC_VERSION = (2026, 7, 1)


def _parse_bsc_version(text: str) -> Optional[tuple[int, int, int]]:
    """The release in bsc's banner, "Bluespec Compiler, version 2026.07.1 (build 63665af1)".

    Most releases have two components (2026.07); a bug-fix release adds a third (2026.07.1).
    """
    match = re.search(r"version\s+(\d+)\.(\d+)(?:\.(\d+))?", text)
    if not match:
        return None
    year, month, patch = match.groups()
    return (int(year), int(month), int(patch or 0))


@lru_cache(maxsize=None)
def _probe_bsc() -> bool:
    """Check the version, then actually compile a trivial BSV package to Verilog.

    `bsc -v` succeeds regardless of version, so an installed-but-too-old bsc would otherwise look
    fine; xeda's bsc/bsc_sim flows are not functional below `_MIN_BSC_VERSION`.
    """
    if not shutil.which("bsc"):
        return False
    try:
        proc = subprocess.run(["bsc", "-v"], capture_output=True, text=True, timeout=60)
    except (OSError, subprocess.SubprocessError):
        return False
    if proc.returncode != 0:
        return False
    version = _parse_bsc_version(proc.stdout) or _parse_bsc_version(proc.stderr)
    if version is None or version < _MIN_BSC_VERSION:
        return False
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "XedaProbe.bsv"
        src.write_text(_TRIVIAL_BSV)
        return _command_succeeds(
            ["bsc", "-verilog", "-g", "mkXedaProbe", "-bdir", ".", "-vdir", ".", "XedaProbe.bsv"],
            cwd=tmp,
        )


def require_bsc() -> None:
    """bsc on PATH, at least `_MIN_BSC_VERSION`, and able to compile BSV to Verilog.

    A bsc older than 2026.07.1 is not functional for xeda's bsc/bsc_sim flows even though it
    runs, so that is called out explicitly rather than left to look like a plain "missing tool".
    """
    version = ".".join(str(v) for v in _MIN_BSC_VERSION)
    _require(
        "bsc",
        _probe_bsc(),
        f"bsc -v (>= {version}; an older bsc is not functional for xeda) + compiling a "
        "trivial BSV module to Verilog",
    )


@lru_cache(maxsize=None)
def _probe_bluesim() -> bool:
    """Compile, link *and run* a trivial Bluesim testbench, as `BscSim` does.

    Compiling and linking succeed even when `libtcl8.6.so` -- which every Bluesim executable
    (and `bluetcl`) needs -- is missing; only actually running the linked binary fails.
    """
    if not (_probe_bsc() and _probe_c_toolchain()):
        return False
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "XedaProbe.bsv"
        src.write_text(_TRIVIAL_BSV_SIM)
        if not _command_succeeds(
            ["bsc", "-sim", "-g", "mkXedaProbe", "-bdir", ".", "-simdir", ".", "XedaProbe.bsv"],
            cwd=tmp,
        ):
            return False
        # The Bluesim link step can be slow; give it more than the default timeout.
        if not _command_succeeds(
            ["bsc", "-sim", "-e", "mkXedaProbe", "-bdir", ".", "-simdir", ".", "-o", "probe"],
            cwd=tmp,
            timeout=300,
        ):
            return False
        return _command_succeeds([str(Path(tmp) / "probe")], cwd=tmp)


def require_bluesim() -> None:
    """`require_bsc` and a C toolchain, plus actually running a linked Bluesim binary."""
    require_bsc()
    require_c_toolchain()
    _require(
        "Bluesim",
        _probe_bluesim(),
        "compiling, linking (`bsc -sim -e ...`) and running a trivial Bluesim testbench",
    )


@lru_cache(maxsize=None)
def _probe_bluesim_evidence() -> bool:
    """Link the shipped generated-call monitor and observe an otherwise quiet finish."""
    from importlib.resources import files

    if not _probe_bluesim():
        return False
    with tempfile.TemporaryDirectory(prefix="xeda-bluesim-evidence-") as tmp:
        root = Path(tmp)
        (root / "XedaProbe.bsv").write_text(
            "package XedaProbe; (* synthesize *) module mkXedaProbe(Empty); "
            "rule done; $finish(0); endrule endmodule endpackage"
        )
        for package, name in (("xeda.flow", "sim_record.h"), ("xeda.flows.bsc", "bluesim_hooks.h")):
            (root / name).write_text(files(package).joinpath("templates/" + name).read_text())
        if not _command_succeeds(
            ["bsc", "-sim", "-g", "mkXedaProbe", "-bdir", ".", "-simdir", ".", "XedaProbe.bsv"],
            cwd=tmp,
        ):
            return False
        if not _command_succeeds(
            [
                "bsc",
                "-sim",
                "-e",
                "mkXedaProbe",
                "-bdir",
                ".",
                "-simdir",
                ".",
                "-o",
                "probe",
                "-Xc++",
                "-include",
                "-Xc++",
                str(root / "bluesim_hooks.h"),
            ],
            cwd=tmp,
            timeout=300,
        ):
            return False
        try:
            result = subprocess.run(
                [str(root / "probe")],
                cwd=tmp,
                capture_output=True,
                timeout=10,
                env={**os.environ, "XEDA_SIM_EVENTS": str(root / "events.jsonl")},
            )
            events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
        except (OSError, ValueError, subprocess.SubprocessError):
            return False
        return result.returncode == 0 and events == [{"kind": "finish", "time": 0}]


def require_bluesim_evidence() -> None:
    """Bluesim with functional generated-call hooks, also required by Linux CI."""
    require_bluesim()
    _require(
        "Bluesim evidence",
        _probe_bluesim_evidence(),
        "linking system-task hooks and observing quiet finish",
    )


@lru_cache(maxsize=None)
def _probe_iverilog() -> bool:
    """Compile *and run* a trivial module, as bsc's `-vsim iverilog` link step does."""
    if not (shutil.which("iverilog") and shutil.which("vvp")):
        return False
    with tempfile.TemporaryDirectory() as tmp:
        src = Path(tmp) / "xeda_probe.v"
        src.write_text(_TRIVIAL_VERILOG_TB)
        if not _command_succeeds(["iverilog", "-o", "probe.vvp", "xeda_probe.v"], cwd=tmp):
            return False
        return _command_succeeds(["vvp", "probe.vvp"], cwd=tmp)


def require_iverilog() -> None:
    _require("iverilog", _probe_iverilog(), "`iverilog` + `vvp` of a trivial module")


# ---------------------------------------------------------------------------------------------
# Fake tools: `tests/fake_tools/` stands in for the proprietary tools. Each runs the TCL script it
# is handed under tclsh with the tool's commands recorded, as `fake_<tool>.calls` in the run
# directory -- so a script the real tool would reject fails the fake as well.
# ---------------------------------------------------------------------------------------------

FAKE_TOOLS_DIR = Path(__file__).parent / "fake_tools"


def use_fake_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the fake tools first on PATH for the rest of the test."""
    loader = FAKE_TOOLS_DIR / "openFPGALoader"
    assert (
        loader.is_file()
        and loader.resolve() == (FAKE_TOOLS_DIR / "fake_fpga_tool.py").resolve()
        and os.access(loader, os.X_OK)
    ), f"missing or incorrect fake openFPGALoader: {loader}; refusing real programmer fallback"
    monkeypatch.setenv("PATH", str(FAKE_TOOLS_DIR) + os.pathsep + os.environ.get("PATH", ""))


# ---------------------------------------------------------------------------------------------
# The fake OpenROAD (`tests/fake_tools/openroad`) runs the Tcl that the OpenROAD flows render,
# under `tclsh`, after `fake_openroad_prelude.tcl`. The prelude defines one command for each
# name of `OPENROAD_COMMANDS` and nothing else, so a command that is not in the list is a Tcl
# error (`invalid command name`), and `fake_calls` fails the test. The real-tool tests compare
# the same list with the `help` of the installed OpenROAD.
# ---------------------------------------------------------------------------------------------

#: Every OpenROAD command that a template may emit, grouped by the stage that first needs it. A
#: template that emits a command which is not here adds it here.
OPENROAD_COMMANDS: tuple[str, ...] = (
    # restart header: libraries, design, constraints, layer and wire parasitics, dont-use cells
    "read_liberty",
    "read_lef",
    "read_verilog",
    "link_design",
    "read_db",
    "read_sdc",
    "set_layer_rc",
    "set_wire_rc",
    "set_dont_use",
    # metrics and queries that the helper procedures use
    "utl::set_metrics_stage",
    "report_tns_metric",
    "report_worst_slack_metric",
    "report_fmax_metric",
    "report_clock_skew_metric",
    "report_erc_metrics",
    "report_power_metric",
    "report_design_area_metrics",
    "report_units_metric",
    "get_lib_cell",
    "get_cells",
    "get_property",
    "get_name",
    "ord::get_db",
    "ord::get_db_block",
    "odb::dbObstruction_destroy",
    # floorplan
    "eliminate_dead_logic",
    "report_design_area",
    "check_setup",
    "initialize_floorplan",
    "read_def",
    "make_tracks",
    "set_global_routing_layer_adjustment",
    "set_routing_layers",
    "set_dont_touch",
    "unset_dont_touch",
    "set_opt_config",
    "set_global_sizing_config",
    "repair_tie_fanout",
    "insert_tiecells",
    "create_blockage",
    "create_obstruction",
    "replace_arith_modules",
    "remove_buffers",
    # tap cells and power grid
    "tapcell",
    "cut_rows",
    "add_global_connection",
    "global_connect",
    "set_voltage_domain",
    "define_pdn_grid",
    "add_pdn_stripe",
    "add_pdn_ring",
    "check_power_grid",
    "add_pdn_connect",
    "pdngen",
    # global placement and pin placement
    "gpl::get_global_placement_uniform_density",
    "global_placement",
    "all_pins_placed",
    "place_pin",
    "place_pins",
    "set_place_config",
    "reset_place_config",
    "exclude_io_pin_region",
    "set_io_pin_constraint",
    "set_pin_length",
    "set_pin_length_extension",
    "set_pin_thick_multiplier",
    "set_simulated_annealing",
    "placement_cluster",
    "cluster_flops",
    "buffer_ports",
    "estimate_parasitics",
    # resizing and detailed placement
    "repair_design",
    "repair_timing",
    "report_floating_nets",
    "set_placement_padding",
    "detailed_placement",
    "improve_placement",
    "optimize_mirroring",
    "balance_row_usage",
    "check_placement",
    # clock tree synthesis
    "repair_clock_inverters",
    "clock_tree_synthesis",
    "set_cts_config",
    "configure_cts_characterization",
    "report_cts",
    "repair_clock_nets",
    # global route
    "pin_access",
    "set_global_routing_random",
    "set_macro_extension",
    "set_global_routing_region_adjustment",
    "set_routing_alpha",
    "set_nets_to_route",
    "ord::microns_to_dbu",
    "global_route",
    "set_propagated_clock",
    "all_clocks",
    "repair_antennas",
    "check_antennas",
    "write_guides",
    # detailed route and fill
    "detailed_route",
    "detailed_route_set_default_via",
    "detailed_route_set_unidirectional_layer",
    "create_ndr",
    "assign_ndr",
    "set_debug_level",
    "grt::have_routes",
    "design_is_routed",
    "filler_placement",
    # final outputs and report
    "write_db",
    "write_sdc",
    "write_def",
    "write_verilog",
    "write_cdl",
    "write_sdf",
    "write_abstract_lef",
    "write_timing_model",
    "define_process_corner",
    "set_extraction_rules_file",
    "extract_parasitics",
    "adjust_rc",
    "write_spef",
    "read_spef",
    "set_pdnsim_net_voltage",
    "analyze_power_grid",
    "set_pdnsim_source_settings",
    "set_pdnsim_inst_power",
    "write_pg_spice",
    "insert_decap",
    "report_cell_usage",
    "set_clock_latency",
    "report_checks",
    "report_clock_skew",
    "report_power",
    # images (OpenROAD with a graphical interface)
    "ord::openroad_gui_compiled",
    "ord::dbu_to_microns",
    "gui::show",
    "gui::save_display_controls",
    "gui::restore_display_controls",
    "gui::select_marker_category",
    "gui::clear_highlights",
    "gui::clear_selections",
    "gui::fit",
    "gui::set_display_controls",
    "gui::set_heatmap",
    "gui::show_widget",
    "gui::hide_widget",
    "gui::select_clockviewer_clock",
    "gui::show_worst_path",
    "get_clocks",
    "get_scenes",
    "select",
    "save_image",
    "save_clocktree_image",
)


def use_fake_openroad(monkeypatch: pytest.MonkeyPatch) -> None:
    """Put the fake OpenROAD, with the other fake tools, first on PATH for the rest of the test.

    The fake runs the Tcl script it is given under `tclsh` (`require_tclsh`) and records the
    commands in `fake_openroad.calls` in its working directory (`fake_calls`). Without `tclsh`
    it runs nothing, as the other Tcl fakes do. `XEDA_FAKE_TOOL_FAIL`, `XEDA_FAKE_TOOL_RETURNS`
    (`fake_returns`) and `XEDA_FAKE_OPENROAD_METRICS` (the directory of the recorded metrics
    files) change what it does."""
    fake = FAKE_TOOLS_DIR / "openroad"
    assert (
        fake.is_file()
        and fake.resolve() == (FAKE_TOOLS_DIR / "fake_tool.py").resolve()
        and os.access(fake, os.X_OK)
    ), f"missing or incorrect fake openroad: {fake}"
    use_fake_tools(monkeypatch)


def use_fake_fpga_tools(monkeypatch: pytest.MonkeyPatch, prefix: Path) -> Path:
    """Install a tiny openXC7 prefix in test scratch space and select only fake FPGA tools.

    Yosys is opt-in here so ordinary tests retain their real synthesis/tool probes.
    The share layout and mappings match openXC7 1.0; the generator and assembler
    model structural chipdb validation, not a routable device database.
    """
    use_fake_tools(monkeypatch)
    dispatcher = FAKE_TOOLS_DIR / "fake_fpga_tool.py"
    binary = prefix / "bin"
    binary.mkdir(parents=True, exist_ok=True)
    for name in (
        "yosys",
        "yosys-config",
        "nextpnr-himbaechel",
        "nextpnr-ecp5",
        "nextpnr-ice40",
        "nextpnr-nexus",
        "fpga-as",
        "ecppack",
        "icepack",
        "openFPGALoader",
        "bbasm",
    ):
        path = binary / name
        if not path.exists():
            # A copy preserves the fake installation prefix when the binary is resolved.
            shutil.copyfile(dispatcher, path)
            path.chmod(0o755)
    share = prefix / "share/nextpnr"
    generator = share / "himbaechel/uarch/xilinx/gen/xilinx_gen.py"
    generator.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(dispatcher, generator)
    files = {
        "himbaechel/uarch/xilinx/constids.inc": "X(XEDA_FAKE)\n",
        "himbaechel/uarch/xilinx/meta/artix7/site.json": "{}\n",
        "himbaechel/himbaechel_dbgen/__init__.py": "# fake dbgen package\n",
        "prjxray-db/artix7/mapping/parts.yaml": (
            "xc7a100tcsg324-1:\n  device: xc7a100t\n  package: csg324\n  speedgrade: '1'\n"
            "xc7a35tcsg324-1:\n  device: xc7a35t\n  package: csg324\n  speedgrade: '1'\n"
            "xc7a35tcpg236-1:\n  device: xc7a35t\n  package: cpg236\n  speedgrade: '1'\n"
        ),
        "prjxray-db/artix7/mapping/devices.yaml": (
            "xc7a100t:\n  fabric: xc7a100t\nxc7a35t:\n  fabric: xc7a50t\n"
        ),
        "prjxray-db/artix7/xc7a100t/tilegrid.json": "{}\n",
        "prjxray-db/artix7/xc7a50t/tilegrid.json": "{}\n",
        # the directory `fpga-as` finds the data of each part in
        **{
            f"prjxray-db/artix7/{part}/{name}": "{}\n"
            for part in ("xc7a100tcsg324-1", "xc7a35tcsg324-1", "xc7a35tcpg236-1")
            for name in ("part.json", "part.yaml", "package_pins.csv")
        },
    }
    for name, content in files.items():
        path = share / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    yosys_share = prefix / "share/yosys"
    yosys_files = {
        "xilinx/cells_sim.v": "// fake Xilinx simulation primitives\n",
        "xilinx/cells_xtra.v": "// fake Xilinx extra primitives\n",
        "ice40/cells_sim.v": "// fake iCE40 simulation primitives\n",
        "lattice/cells_sim_ecp5.v": "// fake ECP5 simulation primitives\n",
        "lattice/cells_bb_ecp5.v": "// fake ECP5 black boxes\n",
        "lattice/cells_sim_nexus.v": "// fake Nexus simulation primitives\n",
        "lattice/cells_bb_nexus.v": "// fake Nexus black boxes\n",
        "gowin/cells_sim.v": "// fake Gowin simulation primitives\n",
        "gowin/cells_xtra_gw1n.v": "// fake Gowin GW1N extra primitives\n",
        "gowin/cells_xtra_gw2a.v": "// fake Gowin GW2A extra primitives\n",
        "gowin/cells_xtra_gw5a.v": "// fake Gowin GW5A extra primitives\n",
    }
    for name, content in yosys_files.items():
        path = yosys_share / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    return prefix


def use_fake_asic_tools(monkeypatch: pytest.MonkeyPatch, binary: Path) -> Path:
    """Put a fake `yosys` (`fake_tools/fake_asic_tool.py`) first on `PATH`, in `binary`, a
    directory of the test's own. It is not among `FAKE_TOOLS_DIR`'s: the tests that run the real
    yosys must keep finding it there. What it writes follows from the contents of the files it is
    handed, so a flow that hands it another library or another netlist gets another result, and
    the same request in another directory the same bytes."""
    use_fake_tools(monkeypatch)
    dispatcher = FAKE_TOOLS_DIR / "fake_asic_tool.py"
    binary.mkdir(parents=True, exist_ok=True)
    for name in ("yosys",):
        link = binary / name
        if not link.exists():
            link.symlink_to(dispatcher)
    monkeypatch.setenv("PATH", str(binary) + os.pathsep + os.environ["PATH"])
    return binary


def fake_returns(monkeypatch: pytest.MonkeyPatch, returns: dict[tuple[str, ...], str]) -> None:
    """Make the fake tools answer each call that begins with the given words with the given text
    (`XEDA_FAKE_TOOL_RETURNS`): what the real tool would report in a case the fake does not reach
    on its own, such as a Vivado run reporting `{("get_property", "STATUS", "impl_1"):
    "write_bitstream Complete!"}` although its step failed. The longest match wins."""
    for words, value in returns.items():  # braced as they are, so none may hold a brace
        assert all(re.fullmatch(r"[\w.:-]+", word) for word in words), words
        assert not set("{}\\") & set(value), value
    monkeypatch.setenv(
        "XEDA_FAKE_TOOL_RETURNS",
        " ".join(f"{{{' '.join(words)}}} {{{value}}}" for words, value in returns.items()),
    )


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
#   XEDA_TESTS_EXTERNAL=1  bsc flows on external repositories (tests/test_bsc_external.py), with
#     XEDA_TESTS_EXTERNAL_SLOW=1 for its slowest test too
#   XEDA_TESTS_OPENXC7=1  FPGA builds by the real openXC7 1.0 toolchain (tests/test_openxc7_real.py)
#   XEDA_TESTS_OPENROAD=1  the OpenROAD flows run by a real `openroad`
#     (tests/test_openroad_real.py); the tests that load a platform need the PDK store below too
#   XEDA_TESTS_YOSYS_TCL=1  Tcl scripts run by a real `yosys` with Tcl support: the one on PATH
#     (tests/test_yosys_asic_tcl.py). Most builds of OSS CAD Suite have none.
#   XEDA_TESTS_PDK_ROOT=<directory>  each bundled platform against its real PDK files, in a PDK
#     store; XEDA_TESTS_PLATFORMS=<names> lists the bundled platforms that store holds
#     (tests/test_platform_conformance.py)
#   XEDA_TESTS_ASIC_QOR=1  the ASIC flows of xeda held to the results of OpenROAD-flow-scripts on
#     the same designs (tests/test_asic_parity.py, with tools/asic_parity.py); needs
#     XEDA_TESTS_ORFS=<checkout of OpenROAD-flow-scripts>, an `openroad`, and a `yosys` with Tcl
#     and the slang plugin on PATH. The checkout holds the platform files, so no PDK store.
#   XEDA_TESTS_ORFS alone runs the fast checks against that checkout
#     (tests/test_parity_override_sky130hd.py).
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


def require_openxc7() -> Path:
    """Run the openXC7 toolchain only when asked (`XEDA_TESTS_OPENXC7=1`: its first build
    generates a chip database, a minute and gigabytes of memory), and then insist on it: the
    installation prefix `PATH` selects, with its yosys, nextpnr-himbaechel with the Xilinx
    backend, chip database generator, Project X-Ray database and fpga-as. Its own variable,
    not `XEDA_TESTS_REQUIRE_TOOLS`: the tox legs of CI require the general tools and have no
    openXC7. Only the workflow `openxc7.yml` sets this variable.
    `openFPGALoader` is not looked for: nothing here programs a device."""
    if not _opted_in("XEDA_TESTS_OPENXC7"):
        pytest.skip("set XEDA_TESTS_OPENXC7=1 to run the tests that run the openXC7 toolchain")
    from xeda.flow import FlowFatalError
    from xeda.flows.xilinx import find_xilinx_layout, select_xilinx

    nextpnr = shutil.which("nextpnr-himbaechel")
    if nextpnr is None:
        pytest.fail("XEDA_TESTS_OPENXC7 is set, but no `nextpnr-himbaechel` is on PATH")
    prefix = Path(nextpnr).resolve().parent.parent
    try:
        layout = find_xilinx_layout(Path(nextpnr))
        select_xilinx("xc7a100tcsg324-1", layout)
    except FlowFatalError as error:
        pytest.fail(
            f"XEDA_TESTS_OPENXC7 is set, but {nextpnr} is not openXC7's (put its `bin` "
            f"directory first on PATH): {error}"
        )
    for tool, probe in (
        ("yosys", ["yosys", "-V"]),
        ("nextpnr-himbaechel", ["nextpnr-himbaechel", "--version"]),
        ("fpga-as", None),  # it has no option that exits with zero status without packing
    ):
        found = shutil.which(tool)
        if found is None or Path(found).resolve().parent != prefix / "bin":
            pytest.fail(
                f"XEDA_TESTS_OPENXC7 is set, but `{tool}` on PATH is {found}, not openXC7's"
            )
        if probe is not None and not _command_succeeds(probe):
            pytest.fail(f"XEDA_TESTS_OPENXC7 is set, but `{' '.join(probe)}` fails")
    return prefix


@lru_cache(maxsize=None)
def _probe_openroad(path_variable: str) -> bool:
    """Run `openroad -version` and then a one-line script the way the flows start the tool
    (`-no_init -no_splash -exit`), in a temporary directory. The cache key is the value of `PATH`,
    so a different `PATH` finds a different `openroad`."""
    if not _command_succeeds(["openroad", "-version"]):
        return False
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "probe.tcl"
        script.write_text("puts xeda_probe\n")
        return _command_succeeds(
            ["openroad", "-no_init", "-no_splash", "-exit", str(script)], cwd=tmp
        )


def _find_openroad(variable: str) -> Path:
    """Return the `openroad` on `PATH`. Fail, naming `variable`, if there is none or it does not
    run."""
    found = shutil.which("openroad")
    if found is None:
        pytest.fail(f"{variable} is set, but no `openroad` is on PATH")
    if not _probe_openroad(os.environ.get("PATH", "")):
        pytest.fail(
            f"{variable} is set, but the `openroad` on PATH ({found}) does not run: "
            "`openroad -version` or a script with `-no_init -no_splash -exit` fails"
        )
    return Path(found)


def require_openroad() -> Path:
    """Run the OpenROAD flows against the real tool only when asked (`XEDA_TESTS_OPENROAD=1`: a
    chain of the flows takes minutes), and then insist on an `openroad` on `PATH` that runs.
    Return its path. Its own variable, not `XEDA_TESTS_REQUIRE_TOOLS`: the tox legs of CI require
    the general tools and have no OpenROAD."""
    if not _opted_in("XEDA_TESTS_OPENROAD"):
        pytest.skip("set XEDA_TESTS_OPENROAD=1 to run the tests that run OpenROAD")
    return _find_openroad("XEDA_TESTS_OPENROAD")


@lru_cache(maxsize=None)
def _probe_yosys_tcl(path_variable: str) -> bool:
    """Run a one-line Tcl script with `yosys -c`, in a temporary directory. The cache key is the
    value of `PATH`, so a different `PATH` finds a different `yosys`."""
    with tempfile.TemporaryDirectory() as tmp:
        script = Path(tmp) / "probe.tcl"
        script.write_text("puts xeda_probe\n")
        return _command_succeeds(["yosys", "-q", "-c", str(script)], cwd=tmp)


@lru_cache(maxsize=None)
def _probe_yosys_slang(path_variable: str) -> bool:
    """Load the slang plugin with `yosys -p`, in a temporary directory. A yosys that has `slang`
    built in loads it too. The cache key is the value of `PATH`."""
    with tempfile.TemporaryDirectory() as tmp:
        return _command_succeeds(["yosys", "-q", "-p", "plugin -i slang"], cwd=tmp)


def _check_yosys_tcl(variable: str) -> None:
    if not _probe_yosys_tcl(os.environ.get("PATH", "")):
        pytest.fail(
            f"{variable} is set, but the `yosys` on PATH does not run a Tcl script "
            "with `yosys -c`"
        )


def require_yosys_tcl() -> None:
    """Run a Tcl script with yosys only when asked (`XEDA_TESTS_YOSYS_TCL=1`), and then insist on
    a `yosys` on `PATH` that runs one with `yosys -c`. Its own variable, not
    `XEDA_TESTS_REQUIRE_TOOLS`: the OSS CAD Suite of the general tox legs has no Tcl in its
    yosys."""
    if not _opted_in("XEDA_TESTS_YOSYS_TCL"):
        pytest.skip("set XEDA_TESTS_YOSYS_TCL=1 to run the tests that run a yosys with Tcl")
    _check_yosys_tcl("XEDA_TESTS_YOSYS_TCL")


class AsicQorTools(NamedTuple):
    """What the comparison with OpenROAD-flow-scripts runs: the checkout, `openroad` and `yosys`."""

    orfs: Path
    openroad: Path
    yosys: Path


def require_orfs_checkout() -> Path:
    """Return the checkout of OpenROAD-flow-scripts that `XEDA_TESTS_ORFS` names, and skip without
    the variable. A value that names no checkout fails: the variable was set for a reason."""
    value = os.environ.get("XEDA_TESTS_ORFS", "")
    if not value:
        pytest.skip("set XEDA_TESTS_ORFS to a checkout of OpenROAD-flow-scripts")
    orfs = Path(value).expanduser()
    if not (orfs / "flow" / "Makefile").is_file():
        pytest.fail(
            f"XEDA_TESTS_ORFS is {value}, which is no checkout of OpenROAD-flow-scripts "
            "(it has no flow/Makefile)"
        )
    return orfs


def require_asic_qor() -> AsicQorTools:
    """Hold the ASIC flows of xeda to OpenROAD-flow-scripts only when asked
    (`XEDA_TESTS_ASIC_QOR=1`: a design takes minutes, and the largest takes an hour), and then
    insist on its tools. `XEDA_TESTS_ORFS` names a checkout of OpenROAD-flow-scripts. `PATH` holds
    an `openroad` that runs and a `yosys` that runs a Tcl script and loads the slang plugin: the
    flows of ORFS and of xeda both use that yosys. The checkout holds the platform files, so the
    layer needs no PDK store. Its own variable, not `XEDA_TESTS_REQUIRE_TOOLS`."""
    if not _opted_in("XEDA_TESTS_ASIC_QOR"):
        pytest.skip(
            "set XEDA_TESTS_ASIC_QOR=1 and XEDA_TESTS_ORFS to hold the ASIC flows to "
            "OpenROAD-flow-scripts"
        )
    if not os.environ.get("XEDA_TESTS_ORFS", ""):
        pytest.fail(
            "XEDA_TESTS_ASIC_QOR is set, but XEDA_TESTS_ORFS is not: give a checkout of "
            "OpenROAD-flow-scripts"
        )
    orfs = require_orfs_checkout()
    openroad = _find_openroad("XEDA_TESTS_ASIC_QOR")
    yosys = shutil.which("yosys")
    if yosys is None:
        pytest.fail("XEDA_TESTS_ASIC_QOR is set, but no `yosys` is on PATH")
    _check_yosys_tcl("XEDA_TESTS_ASIC_QOR")
    if not _probe_yosys_slang(os.environ.get("PATH", "")):
        pytest.fail(
            "XEDA_TESTS_ASIC_QOR is set, but the `yosys` on PATH does not load the slang plugin "
            "(`yosys -p 'plugin -i slang'` fails)"
        )
    return AsicQorTools(orfs, openroad, Path(yosys))


def require_pdk_store() -> Path:
    """Test against the real PDK files only when asked (`XEDA_TESTS_PDK_ROOT=<directory>`), and
    then insist on a PDK store there. Return the directory."""
    value = os.environ.get("XEDA_TESTS_PDK_ROOT", "")
    if not value:
        pytest.skip("set XEDA_TESTS_PDK_ROOT to a PDK store to test the platforms against it")
    store = Path(value).expanduser()
    if not store.is_dir():
        pytest.fail(f"XEDA_TESTS_PDK_ROOT is set, but {value} is not a directory")
    return store


def pdk_test_platforms() -> List[str]:
    """Return the bundled platforms that the PDK store of `XEDA_TESTS_PDK_ROOT` holds, as
    `XEDA_TESTS_PLATFORMS` lists them (comma-separated). A job that holds one platform tests one.

    Without `XEDA_TESTS_PDK_ROOT`, return every bundled platform, so that pytest collects the
    tests of each one and each skips with the variable named (`require_pdk_store`). With it,
    `XEDA_TESTS_PLATFORMS` is required, and each name in it must be a bundled platform."""
    from xeda.platforms import bundled_platform_names

    bundled = bundled_platform_names()
    if not os.environ.get("XEDA_TESTS_PDK_ROOT", ""):
        return bundled
    listed = os.environ.get("XEDA_TESTS_PLATFORMS")
    if listed is None:
        pytest.fail(
            "XEDA_TESTS_PDK_ROOT is set, but XEDA_TESTS_PLATFORMS is not: "
            "list the bundled platforms that the store holds, separated by commas "
            f"({', '.join(bundled)})"
        )
    names = [name.strip() for name in listed.split(",")]
    unknown = [name for name in names if name not in bundled]
    if unknown:
        shown = ", ".join(repr(name) for name in unknown)
        pytest.fail(
            f"XEDA_TESTS_PLATFORMS names {shown}, which is no bundled platform "
            f"(the bundled platforms are: {', '.join(bundled)})"
        )
    return names


def pdk_test_pairs() -> List[Tuple[str, Optional[str]]]:
    """Return the (platform, provider) pairs that the PDK conformance tests run. Each platform of
    `pdk_test_platforms()` comes with its default provider (the provider `None`). The variable
    `XEDA_TESTS_PDK_PROVIDERS` (comma-separated `platform:provider`, for example
    `sky130hd:lambdapdk`) adds the pairs of another provider that the same store holds.

    Without `XEDA_TESTS_PDK_ROOT`, every provider with its own layout of a bundled platform is
    listed, so that pytest collects the tests and each skips with the variable named. With it, an
    entry must name a listed platform and a provider that serves it, other than the default one
    and `directory`; anything else fails."""
    from xeda.platforms import load_residue

    platforms = pdk_test_platforms()
    pairs: List[Tuple[str, Optional[str]]] = [(name, None) for name in platforms]
    listed = os.environ.get("XEDA_TESTS_PDK_PROVIDERS", "")
    if not os.environ.get("XEDA_TESTS_PDK_ROOT", ""):
        for name in platforms:
            residue = load_residue(name)
            for provider in ("ciel", "orfs", "lambdapdk"):
                if provider != residue.providers.default and residue.providers.entry(provider):
                    pairs.append((name, provider))
        return pairs
    for part in listed.split(",") if listed else []:
        item = part.strip()
        name, _, provider = item.partition(":")
        if not name or not provider or ":" in provider:
            pytest.fail(f"XEDA_TESTS_PDK_PROVIDERS has `{item}`: write platform:provider")
        if name not in platforms:
            pytest.fail(
                f"XEDA_TESTS_PDK_PROVIDERS names the platform {name!r}, which "
                f"XEDA_TESTS_PLATFORMS does not list ({', '.join(platforms)})"
            )
        residue = load_residue(name)
        if (
            provider in ("directory", residue.providers.default)
            or residue.providers.entry(provider) is None
        ):
            pytest.fail(
                f"XEDA_TESTS_PDK_PROVIDERS names the provider {provider!r} for {name}: give a "
                "provider other than the default one and `directory` that serves the platform"
            )
        if (name, provider) in pairs:
            pytest.fail(f"XEDA_TESTS_PDK_PROVIDERS lists {item} twice")
        pairs.append((name, provider))
    return pairs


def pdk_pair_id(pair: Tuple[str, Optional[str]]) -> str:
    """The test id of a (platform, provider) pair."""
    name, provider = pair
    return name if provider is None else f"{name}-{provider}"


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


@lru_cache(maxsize=None)
def require_modelsim() -> None:
    """Opt-in Docker capability check: compile/run quiet finish and read native proof.

    A version banner is insufficient. Never start Docker or pull an image here; a caller
    explicitly enables the proprietary layer, and an incompatible installed image fails.
    """
    from xeda import Design
    from xeda.flow_runner import DefaultRunner
    from xeda.flows.modelsim import Modelsim, ModelsimTool

    docker = ModelsimTool.model_fields["docker"].default
    assert docker is not None
    image = docker.image if ":" in docker.image else f"{docker.image}:{docker.tag or 'latest'}"
    require_docker_image(image)
    work = checkout_work_dir("modelsim_capability_")
    try:
        source = work / "probe.sv"
        source.write_text("module probe; initial $finish(0); endmodule\n")
        design = Design(
            name="probe",
            design_root=work,
            rtl={"sources": [source], "top": "probe"},
            tb={"top": "probe"},
        )
        flow = DefaultRunner(work / "runs", display_results=False).run_flow(
            Modelsim, design, {"dockerized": True, "timeout": 60}
        )
        if flow is None or not flow.succeeded or flow.results.get("sim.ended_by") != "finish":
            pytest.fail(f"{image} did not provide native quiet-finish runtime evidence")
        if flow.results.get("sim.time") != 0:
            pytest.fail(f"{image} did not provide native time/precision")
    finally:
        shutil.rmtree(work, ignore_errors=True)


def checkout_work_dir(prefix: str) -> Path:
    """A fresh directory for a test that runs a tool elsewhere -- in a container, or through a
    `vivado` wrapper that runs one: under `XEDA_TESTS_WORK_DIR` if set, else in the checkout's
    `xeda_run/`, which such a container can mount where the system temp directory is not."""
    base = Path(os.environ.get("XEDA_TESTS_WORK_DIR") or Path(__file__).parent.parent / "xeda_run")
    base.mkdir(parents=True, exist_ok=True)
    return Path(tempfile.mkdtemp(prefix=prefix, dir=base))


def yosys_json_attribute_holders(netlist: Path, attribute: str) -> list[str]:
    """Everything in the yosys JSON netlist `netlist` that carries `attribute`: modules (library
    boxes included), cells, memories and wires, each as a `/`-separated path into the JSON."""

    def holders(node: object, path: str) -> Iterator[str]:
        if isinstance(node, dict):
            attributes = node.get("attributes")
            if isinstance(attributes, dict) and attribute in attributes:
                yield path
            for key, value in node.items():
                yield from holders(value, f"{path}/{key}" if path else str(key))

    return list(holders(json.loads(netlist.read_text()), ""))


#: How a stale reason begins for an input the last run was found to read only afterwards (a
#: depfile's entry), on another file system than its run directory's, of which no record from
#: before that run exists: whether it changed during the run is unknown, and no clock of another
#: file system decides it (`xeda.digest.UNRECORDED_BEFORE_RUN`).
UNRECORDED_BEFORE_RUN_REASON = "input first read by the last run, on another file system"


def launch_until_fresh(runner: Any, launch: Any) -> Any:
    """`launch()`, which launches a flow through `runner` that ran before, until the flow is found
    up to date -- at most once more: a tool reporting what it read (`yosys -E`) names files of
    its own installation, which lie on another file system than the run directory on some
    machines (a macOS volume, say), and with no record of them from before the first run, the
    next launch runs once more, saying so. The fresh flow."""
    first = len(runner.launched)
    flow = launch()
    if flow.reused:
        return flow
    reasons = [f.stale_reason for f in runner.launched[first:]]
    assert any(r and r.startswith(UNRECORDED_BEFORE_RUN_REASON) for r in reasons), reasons
    flow = launch()
    assert flow.reused, flow.stale_reason
    return flow


def producers_of(runner: Any, flow: Any) -> list[Any]:
    """The flows `flow` took its declared inputs from, in input order and each once: the latest
    launched flows (`runner.launched`) whose run directories its recorded inputs name."""
    wanted = [
        reference.producer_path
        for record in flow.declared_input_records
        for reference in record.references
    ]
    found: list[Any] = []
    for path in dict.fromkeys(wanted):
        found.append(next(f for f in reversed(runner.launched) if str(f.run_path) == path))
    return found


def check_after_the_racy_window(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the next freshness check see every file as one that changed long before it was
    recorded, and the check as happening long after: a record that was racy when it was taken
    is read by content, found unchanged, and -- having settled since -- the trace is refreshed
    (`trace.check_trace`, `Freshness.refreshed`). What a slow machine does by itself, between two
    runs more than `digest.RACY_NS` apart, without the wait or the luck."""
    from xeda import digest
    from xeda.flow_runner import trace

    window = 10**12
    monkeypatch.setattr(digest, "RACY_NS", window)
    real = trace.filesystem_time_ns
    monkeypatch.setattr(trace, "filesystem_time_ns", lambda directory: real(directory) + 3 * window)


def run_outputs_state(*run_dirs: Path) -> dict[Path, tuple[Any, ...]]:
    """What each run directory holds that a run's tools and flow left there: every entry but the
    names xeda reserves for itself (`trace.RESERVED_FILES`, the clock markers), a file by its
    size, modification time, inode and content, a link by its target, a directory by being one.
    Two states are equal when a launch left every output alone. A launch that found the
    directory fresh may still rewrite the trace (it refreshes the records of a file that settled,
    `trace.check_trace`), so a test that says "nothing was rewritten" compares this, never a
    listing of the whole directory."""
    from xeda.flow_runner.trace import run_directory_files

    state: dict[Path, tuple[Any, ...]] = {}
    for run_dir in run_dirs:
        for entry in run_directory_files(run_dir):
            status = entry.lstat()
            if stat.S_ISLNK(status.st_mode):
                state[entry] = ("link", os.readlink(entry))
            elif stat.S_ISDIR(status.st_mode):
                state[entry] = ("directory",)
            else:
                content = hashlib.sha256(entry.read_bytes()).hexdigest()
                state[entry] = ("file", status.st_size, status.st_mtime_ns, status.st_ino, content)
    return state
