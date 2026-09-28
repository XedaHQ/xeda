"""End-to-end tests of the `bsc` and `bsc_sim` flows against real, external Bluespec code.

This is an opt-in layer, alongside `XEDA_TESTS_VIVADO`/`XEDA_TESTS_DOCKER` in `tool_utils.py`:
skipped unless `XEDA_TESTS_EXTERNAL=1`, and then insisting on what it needs rather than silently
skipping. It clones three open-source repositories at pinned commits --
`B-Lang-org/bsc-contrib` (self-checking testbenches with golden output), `kammoh/bluelight` (an
Ascon lightweight-crypto core), and `bluespec/Piccolo` (a RV32ACIMU core) -- and runs the `bsc`/
`bsc_sim` flows on them, checking real compilation and, for bsc-contrib, that the simulators
reproduce each line of the golden simulation trace, in order.

Each repository is fetched once per test session, by pinned commit SHA (`git fetch --depth 1
<url> <sha>`, which GitHub serves for a full commit SHA even though the API does not), into a
cache directory: `XEDA_TESTS_EXTERNAL_CACHE` if set, otherwise the checkout's `xeda_run/external/`
(which is git-ignored, like the rest of `xeda_run/`). An existing checkout already at the pinned
commit is reused, across tests and across sessions. A failed fetch is a test failure, not a skip:
under `XEDA_TESTS_EXTERNAL=1` this layer is expected to work.

Even when opted in, a missing or non-functional tool still skips (or, under
`XEDA_TESTS_REQUIRE_TOOLS=1`, fails) through the ordinary `require_bsc`/`require_bluesim`/
`require_verilator`/`require_iverilog` probes in `tool_utils.py` -- this layer only adds which
designs are compiled and simulated, not another way to detect a tool.

CI (`.github/workflows/ci.yml`) sets `XEDA_TESTS_EXTERNAL=1` for one Python version (3.13) only,
since the external repositories are fetched once per CI run regardless of how many Python
versions are tested in it.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

import pytest

from xeda import Design
from xeda.flow_runner import DefaultRunner
from xeda.flows import Bsc, BscSim

from .tool_utils import _opted_in, require_bluesim, require_bsc, require_iverilog, require_verilator

# ---------------------------------------------------------------------------------------------
# the opt-in gate, and fetching pinned commits into a session cache
# ---------------------------------------------------------------------------------------------


def require_external() -> None:
    """Run this layer only when asked (`XEDA_TESTS_EXTERNAL=1`)."""
    if not _opted_in("XEDA_TESTS_EXTERNAL"):
        pytest.skip("set XEDA_TESTS_EXTERNAL=1 to run tests against external Bluespec repositories")


def _external_cache_dir() -> Path:
    """Where pinned-commit checkouts are cached: `XEDA_TESTS_EXTERNAL_CACHE`, or the checkout's
    `xeda_run/external/` (git-ignored, like the rest of `xeda_run/`)."""
    base = Path(
        os.environ.get("XEDA_TESTS_EXTERNAL_CACHE")
        or Path(__file__).parent.parent / "xeda_run" / "external"
    )
    base.mkdir(parents=True, exist_ok=True)
    return base


def _run_git(args: list[str], cwd: Path) -> None:
    proc = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    if proc.returncode != 0:
        raise RuntimeError(
            f"`git {' '.join(args)}` (cwd={cwd}) failed with exit code {proc.returncode}:\n"
            f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
        )


def _checked_out_sha(path: Path) -> str | None:
    """The commit `path` has checked out, or `None` if it is not a (complete) git checkout."""
    if not (path / ".git").is_dir():
        return None
    proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=path, capture_output=True, text=True)
    return proc.stdout.strip() if proc.returncode == 0 else None


def _fetch_pinned_commit(name: str, url: str, sha: str) -> Path:
    """A checkout of `url` at the pinned commit `sha`, from the session cache -- fetched fresh
    only when no cached checkout is already at that exact commit."""
    dest = _external_cache_dir() / f"{name}-{sha[:12]}"
    if _checked_out_sha(dest) == sha:
        return dest
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    _run_git(["init", "-q"], dest)
    _run_git(["fetch", "--depth", "1", url, sha], dest)
    _run_git(["checkout", "-q", "FETCH_HEAD"], dest)
    return dest


BSC_CONTRIB_URL = "https://github.com/B-Lang-org/bsc-contrib"
BSC_CONTRIB_SHA = "1ec02afed0b783f49aa94e4e63752ec1650736be"

BLUELIGHT_URL = "https://github.com/kammoh/bluelight"
BLUELIGHT_SHA = "4b90d3d689969f9fd44919345d2a8a646a8e3639"

PICCOLO_URL = "https://github.com/bluespec/Piccolo"
PICCOLO_SHA = "8a80b63af7e0036833836c91fc95f18f74c5fafa"


@pytest.fixture(scope="session")
def bsc_contrib_repo() -> Path:
    require_external()
    return _fetch_pinned_commit("bsc-contrib", BSC_CONTRIB_URL, BSC_CONTRIB_SHA)


@pytest.fixture(scope="session")
def bluelight_repo() -> Path:
    require_external()
    return _fetch_pinned_commit("bluelight", BLUELIGHT_URL, BLUELIGHT_SHA)


@pytest.fixture(scope="session")
def piccolo_repo() -> Path:
    require_external()
    return _fetch_pinned_commit("piccolo", PICCOLO_URL, PICCOLO_SHA)


# ---------------------------------------------------------------------------------------------
# helpers shared by the tests
# ---------------------------------------------------------------------------------------------


def _run(flow_class: type, design: Design, run_dir: Path, **settings: Any) -> Any:
    return DefaultRunner(run_dir, display_results=False).run_flow(flow_class, design, settings)


def _golden_lines(path: Path) -> list[str]:
    """The non-blank lines of a bsc-contrib `*.out.expected` golden file, trailing whitespace
    stripped."""
    return [line.rstrip() for line in path.read_text().splitlines() if line.strip()]


def _assert_output_contains_in_order(golden: list[str], stdout: str) -> None:
    """Every golden line appears in `stdout`, in that order (a subsequence), so a simulator's own
    extra lines (Verilator's "- ...: Verilog $finish", Icarus's "$finish called at ...") are
    ignored, along with any of xeda's own logging mixed into the same stream."""
    actual = iter(line.rstrip() for line in stdout.splitlines())
    for expected in golden:
        for line in actual:
            if line == expected:
                break
        else:
            pytest.fail(
                f"expected line not found (in order) in the simulator's captured stdout: "
                f"{expected!r}\n--- full captured stdout ---\n{stdout}"
            )


def _elaborate_with_iverilog(top: str, files: list[str], tmp_path: Path) -> None:
    """`bsc`'s Verilog artifacts elaborate on their own, as a downstream synthesis/sim flow
    would read them: `iverilog -g2005 -s <top> <files>` exits 0."""
    out = tmp_path / "elab.out"
    proc = subprocess.run(
        ["iverilog", "-g2005", "-o", str(out), "-s", top, *files],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, (
        f"iverilog failed to elaborate the `bsc` artifacts for {top!r}:\n"
        f"--- stdout ---\n{proc.stdout}\n--- stderr ---\n{proc.stderr}"
    )


# ---------------------------------------------------------------------------------------------
# 1. bsc-contrib: self-checking testbenches with golden output
# ---------------------------------------------------------------------------------------------

# a. AMBA_Fabrics/AXI4: a self-checking AXI4 fabric testbench, run with two simulators.

_AXI4_DIR = "testing/bsc.contrib/AMBA_Fabrics/AXI4"
_AXI4_TB_FILE = f"{_AXI4_DIR}/Test_AXI4_Fabric.bsv"
_AXI4_TOP = "sysTest_AXI4_Fabric"
_AXI4_SEARCH_PATHS = [
    "Libraries/AMBA_Fabrics/AXI4",
    "Libraries/AMBA_Fabrics/Utils",
    "Libraries/Misc",
]

# bsc-contrib's own AXI4 fabric testbench (at the pinned commit) has a pre-existing bug in its
# diagnostic prints: `Test_AXI4_Fabric.bsv` line 432 writes `$display("    M%0d: ERROR: ...")`
# with no argument at all, and lines 566-575 (in its `mkSbox` submodule) write
# `$display("... S%0d: ...", fmt_User_struct(...))` -- one `%0d` format specifier, but the sole
# argument is a `Fmt`, not the `s_num` integer the format string expects. Bluesim never trips
# over this: those prints run only when the testbench's own self-check has *failed*, which never
# happens in a passing run, and Bluesim only evaluates a `$display` it actually executes. Verilator instead statically
# validates every `$display`/`$write` argument count in the generated Verilog regardless of
# whether that branch is ever taken at run time, and fails to compile with e.g.
# "mkSbox.v:512:2: Missing arguments for $display-like format". This reproduces identically
# compiling bsc's own generated Verilog directly with `verilator --lint-only mkSbox.v` --
# nothing to do with xeda's `bsc_sim` flow.
_AXI4_VERILATOR_XFAIL = pytest.mark.xfail(
    reason=(
        "pre-existing bug in bsc-contrib's own AXI4 testbench (Test_AXI4_Fabric.bsv line 432 "
        "and lines 566-575: `$display` with a `%0d` format specifier but no integer argument) "
        "that only Verilator's static $display/$write argument-count check catches "
        "(Bluesim never executes that branch in a passing run); not a xeda bug -- reproduces "
        "with bare `bsc -vsim verilator` / `verilator --lint-only` on the generated Verilog."
    ),
    strict=True,
    # only the testbench's own failure is expected: a tool gone missing under
    # XEDA_TESTS_REQUIRE_TOOLS, or any other error, still fails
    raises=AssertionError,
)


def _axi4_fabric_design(repo: Path) -> Design:
    return Design(
        name="axi4_fabric",
        design_root=repo,
        rtl={"sources": []},
        tb={"sources": [_AXI4_TB_FILE], "top": _AXI4_TOP},
    )


@pytest.mark.parametrize(
    "simulator", ["bluesim", pytest.param("verilator", marks=_AXI4_VERILATOR_XFAIL)]
)
def test_axi4_fabric_reproduces_golden_output(bsc_contrib_repo, tmp_path, capfd, simulator):
    """`bsc_sim` compiles and simulates bsc-contrib's AXI4 fabric self-checking testbench, and
    its golden trace (`sysTest_AXI4_Fabric.out.expected`) appears, in order, in the simulator's
    stdout. Verilator is `xfail` for a pre-existing bug in the upstream testbench, see above."""
    if simulator == "bluesim":
        require_bluesim()
    else:
        require_bsc()
        require_verilator()
    design = _axi4_fabric_design(bsc_contrib_repo)
    flow = _run(BscSim, design, tmp_path, simulator=simulator, search_paths=_AXI4_SEARCH_PATHS)
    assert flow is not None and flow.succeeded
    golden = _golden_lines(bsc_contrib_repo / _AXI4_DIR / "sysTest_AXI4_Fabric.out.expected")
    _assert_output_contains_in_order(golden, capfd.readouterr().out)


# b. SequenceRules: a BH (Bluespec Classic) self-checking testbench, on every simulator.


@pytest.mark.parametrize("simulator", ["bluesim", "verilator", "iverilog"])
def test_sequence_rules_reproduces_golden_output(bsc_contrib_repo, tmp_path, capfd, simulator):
    """`bsc_sim` on bsc-contrib's SequenceRules (BH) testbench reproduces its golden
    cycle-by-cycle trace, in order, on Bluesim and through bsc's Verilog link step."""
    if simulator == "bluesim":
        require_bluesim()
    else:
        require_bsc()
        (require_verilator if simulator == "verilator" else require_iverilog)()
    design = Design(
        name="sequence_rules_test",
        design_root=bsc_contrib_repo,
        rtl={"sources": []},
        tb={
            "sources": ["testing/bsc.contrib/SequenceRules/SequenceRulesTest.bs"],
            "top": "sysSequenceRulesTest",
        },
    )
    flow = _run(
        BscSim, design, tmp_path, simulator=simulator, search_paths=["Libraries/SequenceRules"]
    )
    assert flow is not None and flow.succeeded
    golden = _golden_lines(
        bsc_contrib_repo / "testing/bsc.contrib/SequenceRules/sysSequenceRulesTest.out.expected"
    )
    _assert_output_contains_in_order(golden, capfd.readouterr().out)


# c. COBS: a BH self-checking testbench, Bluesim only.


def test_cobs_reproduces_golden_output(bsc_contrib_repo, tmp_path, capfd):
    """`bsc_sim`/Bluesim on bsc-contrib's COBS (BH) testbench reproduces its golden trace, in
    order."""
    require_bluesim()
    design = Design(
        name="cobs_tests",
        design_root=bsc_contrib_repo,
        rtl={"sources": []},
        tb={"sources": ["testing/bsc.contrib/COBS/COBSTests.bs"], "top": "sysCOBSTests"},
    )
    flow = _run(BscSim, design, tmp_path, simulator="bluesim", search_paths=["Libraries/COBS"])
    assert flow is not None and flow.succeeded
    golden = _golden_lines(bsc_contrib_repo / "testing/bsc.contrib/COBS/sysCOBSTests.out.expected")
    _assert_output_contains_in_order(golden, capfd.readouterr().out)


# ---------------------------------------------------------------------------------------------
# 2. kammoh/bluelight: an Ascon lightweight-crypto core, `bsc` (RTL) only
# ---------------------------------------------------------------------------------------------

_BLUELIGHT_SOURCES = [
    "bluelight/Bus/BusDefines.bsv",
    "bluelight/Bus/BusFIFO.bsv",
    "bluelight/Bus/Bus.bsv",
    "bluelight/LwcApiDefines.bsv",
    "bluelight/BluelightUtils.bsv",
    "bluelight/CryptoCore.bsv",
    "bluelight/LwcApi.bsv",
    "Ascon/AsconRound.bsv",
    "Ascon/AsconCipher.bsv",
    "Ascon/InputLayer.bsv",
    "Ascon/OutputLayer.bsv",
    "Ascon/Ascon.bsv",
    "Ascon/AsconLwc.bsv",
]


@pytest.mark.parametrize(
    "defines",
    [{}, {"ASCON128A": True, "UNROLL_FACTOR": 2}],
    ids=["ascon128", "ascon128a_unroll2"],
)
def test_bluelight_ascon_generates_elaboratable_verilog(bluelight_repo, tmp_path, defines):
    """`bsc` compiles kammoh/bluelight's Ascon LWC core (top module `lwc`) to Verilog that
    elaborates on its own, for the default variant and for the Ascon-128a/unroll=2 variant."""
    require_bsc()
    require_iverilog()
    design = Design(
        name="bluelight_ascon",
        design_root=bluelight_repo,
        rtl={"sources": _BLUELIGHT_SOURCES, "top": "lwc", "defines": defines},
    )
    flow = _run(Bsc, design, tmp_path)
    assert flow is not None and flow.succeeded
    assert flow.results["modules"][0] == "lwc"
    files = [Path(f) for f in flow.artifacts.verilog]
    assert files, "the `bsc` flow produced no Verilog artifacts"
    for f in files:
        assert f.read_text().startswith("`define BSV_POSITIVE_RESET")
    _elaborate_with_iverilog("lwc", [str(f) for f in files], tmp_path)


# ---------------------------------------------------------------------------------------------
# 3. bluespec/Piccolo: a RV32ACIMU core -- slow, the realistic one (~45-70s of bsc elaboration)
# ---------------------------------------------------------------------------------------------

_PICCOLO_SEARCH_PATHS = [
    "src_Core/CPU",
    "src_Core/ISA",
    "src_Core/RegFiles",
    "src_Core/Core",
    "src_Core/Near_Mem_VM",
    "src_Core/PLIC",
    "src_Core/Near_Mem_IO",
    "src_Core/Debug_Module",
    "src_Core/BSV_Additional_Libs",
    "src_Testbench/SoC",
    "src_Testbench/Fabrics/AXI4",
]

_PICCOLO_DEFINES: dict[str, Any] = {
    name: True
    for name in (
        "RV32",
        "ISA_PRIV_M",
        "ISA_PRIV_U",
        "ISA_I",
        "ISA_M",
        "ISA_A",
        "ISA_C",
        "SHIFT_BARREL",
        "MULT_SYNTH",
        "Near_Mem_Caches",
        "FABRIC64",
    )
}


def test_piccolo_core_compiles_and_elaborates(piccolo_repo, tmp_path):
    """`bsc` compiles the Bluespec Piccolo RV32ACIMU core (top module `mkCore`) end to end.

    The slow, realistic case: bsc elaborates Piccolo's full CPU/near-memory/PLIC hierarchy from
    a single top file (`bsc -u` finds the rest through `search_paths`), which alone takes on the
    order of a minute -- there is no synthesis or simulation on top of it here.
    """
    require_bsc()
    require_iverilog()
    design = Design(
        name="piccolo_core",
        design_root=piccolo_repo,
        rtl={"sources": ["src_Core/Core/Core.bsv"], "top": "mkCore", "defines": _PICCOLO_DEFINES},
    )
    flow = _run(
        Bsc,
        design,
        tmp_path,
        search_paths=_PICCOLO_SEARCH_PATHS,
        suppress_warnings=["G0020"],
        warn_action_shadowing=False,
        keep_fires=True,
    )
    assert flow is not None and flow.succeeded
    assert "mkCore" in flow.results["modules"]
    assert len(flow.results["modules"]) >= 15
    files = [str(f) for f in flow.artifacts.verilog]
    _elaborate_with_iverilog("mkCore", files, tmp_path)
