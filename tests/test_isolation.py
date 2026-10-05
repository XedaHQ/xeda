"""Outside the run roots it created, xeda changes nothing but the output paths a launch named
(D21): the isolation oracle (research/run-dir-isolation.md section 8, O1-O4). Everything runs in
scratch copies under `tmp_path`.

What the oracle cannot see:
- A tool outside `FAKED` is stubbed (`run_process` does nothing), so for those flows only xeda's
  own writes and the fake TCL tools' are observed, never what the real tool would write.
- The audit hook (O3) sees this process only, not the tools it starts; O1 and O2 see those
  through the file system.
- Only the start directory's parent is snapshotted (O1) and made read-only (O2): a write
  elsewhere on the file system is seen by O3 alone, and only when it is this process's own.
The opt-in real-tool layers (`XEDA_TESTS_VIVADO`, `XEDA_TESTS_DOCKER`, `XEDA_TESTS_EXTERNAL`) are
where the sweep can be extended to real tools (a follow-up); real GHDL, nvc and bsc already run in
`test_a_cocotb_simulation_leaves_the_design_directory_as_it_was` and
`test_bsc_sim_simulates_the_bluespec_example_on_a_read_only_tree`."""

import ast
import os
import re
import shutil
import stat
import subprocess
import sys
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, List, Optional, Sequence

import pytest
from click.testing import CliRunner

import xeda
from xeda import Design
from xeda.cli import cli
from xeda.flow import SimFlow
from xeda.design import loading_in_run_root
from xeda.flow_runner import DefaultRunner
from xeda.run_root import ensure_run_root

from .settings_samples import flow_classes, minimal_settings
from .tool_utils import (
    FAKE_TOOLS_DIR,
    require_bluesim,
    require_ghdl,
    require_nvc,
    use_fake_tools,
)

SQRT = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt"
#: PR #88's example, for the flows whose tools read Bluespec
GCD = Path(__file__).parent.parent / "examples" / "bluespec" / "gcd"
PACKAGE = Path(xeda.__file__).parent

#: FPGA flows use process fakes, including an opt-in fake synthesis executable.
FPGA_FAKED = {"yosys_fpga", "nextpnr", "fpga_pack", "openfpgaloader"}
#: The flows whose tools have a fake in tests/fake_tools.
FAKED = {
    "vivado_synth",
    "vivado_alt_synth",
    "vivado_project",
    "vivado_sim",
    "vivado_postsynth_sim",
    "vivado_power",
    "quartus",
    "ise_synth",
    "dc",
    "diamond_synth",
    "modelsim",
} | FPGA_FAKED
#: the design most flows run: sqrt, with its cocotb testbench
SQRT_DESIGN = (
    {"sources": ["sqrt.vhdl"], "top": "sqrt", "clock": {"port": "clk"}},
    {"sources": ["tb_sqrt.py"], "cocotb": True},
)
#: `gcd` for the flows whose tools read Bluespec: `bsc_sim` simulates its Bluespec testbench
#: (it rejects a cocotb one before `run()`)
GCD_DESIGN = (
    {
        "sources": ["rtl/GcdEngine.bsv", "rtl/GcdUnit.bsv"],
        "top": "mkGcdUnit",
        "clock": {"port": "CLK"},
    },
    {"sources": ["tb/TbGcdUnit.bsv"], "top": "mkTbGcdUnit"},
)
#: sqrt with a plain VHDL testbench, for the simulators that cannot run a cocotb one (they reject
#: it at launch: `SimFlow.check_design_supported`)
PLAIN_TB_DESIGN = (SQRT_DESIGN[0], {"sources": ["tb_sqrt_plain.vhdl"], "top": "tb_sqrt_plain"})
PLAIN_TB = """library ieee; use ieee.std_logic_1164.all;
entity tb_sqrt_plain is end;
architecture sim of tb_sqrt_plain is begin end;
"""
DESIGNS = {"bsc": GCD_DESIGN, "bsc_sim": GCD_DESIGN}
DESIGNS.update(
    (cls.name, PLAIN_TB_DESIGN)
    for cls, _ in flow_classes()
    if issubclass(cls, SimFlow) and not cls.cocotb_sim_name and cls.name not in DESIGNS
)
#: settings that make a flow reach what it writes (bsc_sim: its Verilator `dump.vcd` handling)
EXTRA_SETTINGS = {
    "yosys_fpga": {"sta": True},
    "ghdl_sim": {"vcd": "dump.vcd"},
    "vivado_sim": {"saif": "sim.saif"},
    "bsc_sim": {"simulator": "verilator", "vcd": "bsc_sim.vcd"},
}
#: a deliverable to name as a location, per flow that has a simple one
LOCATED = {
    "vivado_synth": {"bitstream": "top.bit"},
    "vivado_sim": {"saif": "sim.saif"},
    "ghdl_sim": {"vcd": "dump.vcd"},
    "nvc": {"wave": "dump.fst"},
    "verilator": {"vcd": "dump.vcd"},
    "yosys_fpga": {"netlist_json": "net.json"},
    "yosys": {"netlist_verilog": "net.v"},
    "nextpnr": {"textcfg": "cfg.txt"},
    "fpga_pack": {"bitstream": "top.bit"},
    "ghdl_synth": {"verilog_output": "sqrt.v"},
    "bsc_sim": {"vcd": "bsc_sim.vcd"},
}
#: The flows the sweep does not bring to their `run()`, and why nothing is lost.
UNREACHED = {
    "openroad": "the asap7 platform's liberty files are not shipped",
    "vivado_power": "its vivado_postsynth_sim dependency finds no netlist",
}

CANARIES = sorted(
    {p.name for p in (PACKAGE / "flows").glob("*/templates/*") if p.is_file()}
    | {
        "notes.txt",
        "settings.json",
        "results.json",
        "trace.json",
        "trace.json.tmp",
        ".xeda.lock",
        ".xeda-owned.json",
        # not at the top of the design's directory: a marker there hands it to xeda (a marked
        # run root), where a delivery is refused
        "sqrt/.xeda-run-root",
        "sqrt.delivered.json",
        "env.sh",
        "sqrt.xpr",
        "sqrt.srcs/sources_1/mine.vhd",
        "sqrt.runs/impl_1/mine.txt",
        "xsim.dir/canary.txt",
        "sim_build/canary.d",
        "bobjs/canary.bo",
        "bobjs/canary.ba",
        "gen_rtl/canary.use",
        "gen_rtl/canary.v",
        "gen_rtl/bsv_defines.v",
        "sim_build/canary.use",
        "sim_build/canary.v",
        "dump.vcd",
        "bsc_sim.vcd",
        "diamond_impl/canary.txt",
        "reports/timing.rpt",
        "outputs/netlist.v",
        "checkpoints/canary.dcp",
        "results/canary.odb",
        "sqrt.totCap",
        "work-obj08.cf",
        "wave.opt",
        "chipdb/canary.bin",
        ".xeda_dependencies/canary.txt",
        "sqrt/vivado_synth/canary.txt",
        "Logs/canary.log",
    }
)


@dataclass
class World:
    parent: Path  # holds the start directory and `outside`: its listing is compared too
    work: Path  # the design's directory, where xeda is started
    outside: Path  # what a link in `work` points to
    root: Path  # the run root, `work/xeda_run`
    delivered: Path  # where a launch that names outputs delivers them


def _world(parent: Path) -> World:
    work, outside = parent / "work", parent / "outside"
    work.mkdir(parents=True)
    outside.mkdir()
    for name in ("sqrt.vhdl", "tb_sqrt.py"):
        shutil.copy(SQRT / name, work / name)
    (work / "tb_sqrt_plain.vhdl").write_text(PLAIN_TB)
    for part in ("rtl", "tb"):
        shutil.copytree(GCD / part, work / part)
    for name in CANARIES:
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(f"the user's {name}\n")
    (outside / "target.txt").write_text("outside\n")
    (work / "link_to_outside").symlink_to(outside / "target.txt")
    return World(parent, work, outside, work / "xeda_run", work / "delivered")


def _state(root: Path, exclude: Sequence[Path]) -> dict:
    """Every entry under `root`, as it is -- type, mode, content or link text, and a file's
    modification time (a tool that only touches a file changes it; a directory's changes with
    every entry made in it, which the listing shows) -- but the `exclude`d subtrees."""

    def excluded(path: Path) -> bool:
        return any(path == e or path.is_relative_to(e) for e in exclude)

    state = {}
    for directory, dirs, files in os.walk(root, followlinks=False):
        here = Path(directory)
        dirs[:] = [d for d in dirs if not excluded(here / d)]
        for name in [*dirs, *files]:
            path = here / name
            if excluded(path):
                continue
            st = os.lstat(path)
            if stat.S_ISLNK(st.st_mode):
                what: tuple = ("link", os.readlink(path))
            elif stat.S_ISDIR(st.st_mode):
                what = ("directory",)
            else:
                what = ("file", path.read_bytes(), st.st_mtime_ns)
            state[str(path.relative_to(root))] = (*what, stat.S_IMODE(st.st_mode))
    return state


# --- O3: the audit hook ---------------------------------------------------------------------

_WATCH: dict = {}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def _placed(path: object, dir_fd: object) -> List[object]:
    """`[path]`, unless it is relative to a directory descriptor, which cannot be placed here
    (shutil.rmtree's own removals, audited with the tree's top). An os call audits `dir_fd` as
    -1 when it names none."""
    absolute = isinstance(path, (str, bytes, os.PathLike)) and os.path.isabs(os.fsdecode(path))
    return [path] if dir_fd in (None, -1) or absolute else []


def _written(event: str, args: tuple) -> List[object]:
    """The paths an audited event writes, creates, renames or deletes."""
    if event == "open":
        path, mode, flags = args
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & _WRITE_FLAGS
        )
        return [path] if writes else []
    if event in ("os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime", "shutil.rmtree"):
        return _placed(args[0], args[-1])
    if event in ("os.rename", "os.link"):  # os.replace is audited as os.rename
        return [*_placed(args[0], args[2]), *_placed(args[1], args[3])]
    if event == "os.symlink":
        return _placed(args[1], args[2])
    if event == "shutil.copyfile":
        return [args[1]]
    if event == "os.truncate":
        return [args[0]]
    return []


def _from_deliver() -> bool:
    frame = sys._getframe(2)
    while frame is not None:
        if frame.f_code.co_filename.endswith(os.path.join("xeda", "deliver.py")):
            return True
        frame = frame.f_back
    return False


def _resolved(raw: object) -> Path:
    """An audited path, absolute, its directory resolved but not the entry itself."""
    written = os.path.abspath(os.fsdecode(raw))  # type: ignore[arg-type]
    return Path(os.path.realpath(os.path.dirname(written)), os.path.basename(written))


def _audit(event: str, args: tuple) -> None:
    if not _WATCH:
        return
    world: World = _WATCH["world"]
    for raw in _written(event, args):
        if not isinstance(raw, (str, bytes, os.PathLike)):
            continue  # a file descriptor: opened already, by path
        path = _resolved(raw)
        if not path.is_relative_to(world.parent) or path.is_relative_to(world.root):
            continue
        if path.is_relative_to(world.delivered) and _from_deliver():
            if event == "os.rename" and raw is args[1]:  # a delivery, renamed into place
                _WATCH["delivered"].append(path)
            continue
        _WATCH["violations"].append(f"{event} {path}")


sys.addaudithook(_audit)


@contextmanager
def watching(world: World, delivered: Optional[list] = None) -> Iterator[list]:
    """Audit the launches in the block: yields the violations; `delivered` collects every file
    `xeda/deliver.py` renamed into place."""
    violations: list = []
    _WATCH.update(
        world=world, violations=violations, delivered=[] if delivered is None else delivered
    )
    try:
        yield violations
    finally:
        _WATCH.clear()


def test_the_oracle_sees_every_change_outside_the_run_root(tmp_path):
    """The teeth of O1 and O3: each way of changing a path outside the run root is seen by both,
    a write inside the run root by neither, and a write under a named destination is admitted
    only from `xeda/deliver.py`."""
    world = _world(tmp_path)
    world.root.mkdir()
    world.delivered.mkdir()
    before = _state(world.parent, [world.root])
    work = world.work
    with watching(world) as violations:
        (world.root / "run.txt").write_text("xeda's\n")
        (world.delivered / "out.txt").write_text("not delivered by xeda.deliver\n")
        with open(work / "notes.txt", "a") as f:
            f.write("appended\n")
        os.remove(work / "dump.vcd")
        os.rename(work / "wave.opt", work / "wave.old")
        os.replace(work / "env.sh", work / "trace.json")
        os.mkdir(work / "new")
        os.symlink("sqrt.vhdl", work / "new_link")
        os.chmod(work / "sqrt.vhdl", 0o600)
        os.utime(work / "tb_sqrt.py", (0, 0))
        os.truncate(work / "results.json", 0)
        shutil.copyfile(work / "sqrt.vhdl", work / "copy.vhdl")
        shutil.rmtree(work / "xsim.dir")
    seen = Counter(v.split()[0] for v in violations)
    assert seen == {
        "open": 3,  # notes.txt, delivered/out.txt (not from xeda.deliver), copyfile's destination
        "os.remove": 1,
        "os.rename": 4,  # both paths, of os.rename and of os.replace
        "os.mkdir": 1,
        "os.symlink": 1,
        "os.chmod": 1,
        "os.utime": 1,
        "os.truncate": 1,
        "shutil.copyfile": 1,
        "shutil.rmtree": 1,
        "os.rmdir": 1,  # rmtree's removal of the top, by its path
    }, violations
    assert not any(str(world.root) in v for v in violations)
    after = _state(world.parent, [world.root])
    changed = {k for k in before.keys() | after.keys() if before.get(k) != after.get(k)}
    assert changed == {
        "work/delivered/out.txt",
        "work/notes.txt",
        "work/dump.vcd",
        "work/wave.opt",
        "work/wave.old",
        "work/env.sh",
        "work/trace.json",
        "work/new",
        "work/new_link",
        "work/sqrt.vhdl",  # its mode
        "work/tb_sqrt.py",  # its modification time
        "work/results.json",
        "work/copy.vhdl",
        "work/xsim.dir",
        "work/xsim.dir/canary.txt",
    }


# --- the launches ---------------------------------------------------------------------------


def _launch(flow_class, world: World, monkeypatch, scenario: str, reached: list) -> str:
    """Launch `flow_class` from the design's directory as `scenario` says; how each launch
    ended ("ok", "failed", or the exception's class name)."""
    use_fake_tools(monkeypatch)
    if flow_class.name in FPGA_FAKED:
        from .tool_utils import use_fake_fpga_tools

        ensure_run_root(world.root)
        use_fake_fpga_tools(monkeypatch, world.root / ".fake-toolchain")
    if flow_class.name not in FAKED:
        monkeypatch.setattr("xeda.tool.run_process", lambda *args, **kwargs: "")
        monkeypatch.setattr("xeda.tool.Tool.version_gte", lambda self, *args: True)
    run = flow_class.run
    monkeypatch.setattr(flow_class, "run", lambda self: reached.append(True) or run(self))
    monkeypatch.chdir(world.work)
    rtl, tb = DESIGNS.get(flow_class.name, SQRT_DESIGN)
    design = Design(
        name="sqrt",  # every flow's, so the canaries named after it (`sqrt.xpr`) apply to all
        design_root=world.work,
        rtl=rtl,
        tb=tb,
        language={"vhdl": {"standard": "2008"}},
    )
    settings = {**minimal_settings(flow_class), **EXTRA_SETTINGS.get(flow_class.name, {})}
    launcher: dict = {"display_results": False}
    if scenario == "clean":
        launcher["clean"] = True
    elif scenario == "purge":
        launcher.update(post_cleanup=True, post_cleanup_purge=True)
    elif scenario == "delivered":
        launcher["outputs_to"] = world.delivered
        for key, name in LOCATED.get(flow_class.name, {}).items():
            settings[key] = str(world.delivered / name)
    outcomes = []
    errors = []
    for _ in range(2 if scenario == "twice" else 1):
        try:
            flow = DefaultRunner(world.root, **launcher).launch_flow(flow_class, design, settings)
            outcomes.append("ok" if flow.succeeded else "failed")
        except Exception as e:  # noqa: BLE001 - how it ends is compared, not judged
            outcomes.append(type(e).__name__)
            errors.append(str(e))
    if flow_class.name in FPGA_FAKED:
        assert all(
            outcome == "ok" for outcome in outcomes
        ), f"{flow_class.name} lost positive isolation coverage: {outcomes}: {errors}"
    return ",".join(outcomes)


FLOWS = [cls for cls, _ in flow_classes()]


def _flow(name: str):
    return next(cls for cls in FLOWS if cls.name == name)


def _named(world: World, flow_name: str, scenario: str, delivered: List[Path]) -> set:
    """What a launch may change outside its run root, relative to `world.parent`: each
    destination it named -- a located deliverable, and the `outputs_to` directory itself -- and,
    in that directory, each file delivery wrote there and the directories it made on the way.
    Nothing else in it: a tool's file beside a delivered one is a change."""
    if scenario != "delivered":
        return set()
    named = {world.delivered, *(world.delivered / n for n in LOCATED.get(flow_name, {}).values())}
    for path in delivered:
        named.update(p for p in (path, *path.parents) if p.is_relative_to(world.delivered))
    return {str(p.relative_to(world.parent)) for p in named}


def _sweep(flow_class, world: World, monkeypatch, scenario: str) -> tuple:
    """One launch of the sweep: what changed outside the run root but the destinations it named
    (O1), what the audit hook saw (O3), and whether the flow reached its `run()`."""
    before = _state(world.parent, [world.root])
    reached: list = []
    delivered: List[Path] = []
    with watching(world, delivered) as violations:
        _launch(flow_class, world, monkeypatch, scenario, reached)
    after = _state(world.parent, [world.root])
    named = _named(world, flow_class.name, scenario, delivered)
    changed = sorted(
        k for k in before.keys() | after.keys() if before.get(k) != after.get(k) and k not in named
    )
    return changed, violations, reached


@pytest.mark.parametrize("scenario", ["twice", "clean", "purge", "delivered"])
@pytest.mark.parametrize("flow_class", FLOWS, ids=lambda c: c.name)
def test_nothing_outside_the_run_root_changes_but_what_was_named(
    flow_class, scenario, tmp_path, monkeypatch
):
    """O1 and O3."""
    changed, violations, reached = _sweep(flow_class, _world(tmp_path), monkeypatch, scenario)
    assert not changed, f"{flow_class.name} changed {changed}"
    assert not violations, f"{flow_class.name}: {violations}"
    if scenario == "twice":  # the sweep keeps its teeth
        assert bool(reached) is (flow_class.name not in UNREACHED), UNREACHED.get(flow_class.name)


def _also_runs(flow_class, monkeypatch, command: str) -> None:
    """`flow_class.run` runs `command` in a child process after the flow's own `run()`: a
    tool's write, which only the file system shows (O1), never the audit hook (O3)."""
    run = flow_class.run

    def run_and_command(self):
        run(self)
        subprocess.run(["sh", "-c", command], check=True)

    monkeypatch.setattr(flow_class, "run", run_and_command)


def test_the_sweep_sees_an_extra_file_beside_a_delivered_one(tmp_path, monkeypatch):
    """Only the destinations a launch named are exempt, not the directory they are in: a tool
    that writes a file of its own beside a delivered one fails the sweep."""
    world = _world(tmp_path)
    vivado_sim = _flow("vivado_sim")
    extra = world.delivered / "extra.txt"
    _also_runs(vivado_sim, monkeypatch, f"mkdir -p '{extra.parent}' && echo tool > '{extra}'")
    changed, violations, _ = _sweep(vivado_sim, world, monkeypatch, "delivered")
    assert (world.delivered / "sim.saif").is_file(), "the named output was delivered beside it"
    assert changed == ["work/delivered/extra.txt"]
    assert not violations, "a child process's write: O3 cannot see it"


def test_the_sweep_sees_a_touch_only_change(tmp_path, monkeypatch):
    """A tool that only sets the modification time of a file of the user's fails the sweep."""
    world = _world(tmp_path)
    vivado_synth = _flow("vivado_synth")
    _also_runs(vivado_synth, monkeypatch, f"touch -m -t 200001010000 '{world.work / 'notes.txt'}'")
    changed, violations, _ = _sweep(vivado_synth, world, monkeypatch, "clean")
    assert changed == ["work/notes.txt"]
    assert not violations, "a child process's write: O3 cannot see it"


def _freeze(root: Path, keep: Path) -> None:
    for directory, dirs, files in os.walk(root, topdown=False, followlinks=False):
        for name in [*files, *dirs]:
            path = Path(directory) / name
            if path == keep or path.is_relative_to(keep) or path.is_symlink():
                continue
            path.chmod(0o555 if path.is_dir() else 0o444)
    root.chmod(0o555)


def _thaw(root: Path) -> None:
    root.chmod(0o755)
    for directory, dirs, files in os.walk(root, followlinks=False):
        for name in [*dirs, *files]:
            path = Path(directory) / name
            if not path.is_symlink():
                path.chmod(0o755 if path.is_dir() else 0o644)


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through file permissions")
@pytest.mark.parametrize("flow_class", FLOWS, ids=lambda c: c.name)
def test_a_launch_needs_nothing_writable_but_its_run_root(flow_class, tmp_path, monkeypatch):
    """O2: with the user's tree read-only (but the run root), a launch ends as it does with a
    writable tree."""
    expected = _launch(flow_class, _world(tmp_path / "rw"), monkeypatch, "once", [])
    world = _world(tmp_path / "ro")
    ensure_run_root(world.root)
    _freeze(world.parent, keep=world.root)
    try:
        assert _launch(flow_class, world, monkeypatch, "once", []) == expected
    finally:
        _thaw(world.parent)


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes through file permissions")
def test_bsc_sim_simulates_the_bluespec_example_on_a_read_only_tree(tmp_path, monkeypatch):
    """O2 with the real tools (gpt-6-sol's final (d)): bsc compiles PR #88's `gcd` and Bluesim
    runs its Bluespec testbench, everything but the run root read-only."""
    require_bluesim()
    world = _world(tmp_path)
    ensure_run_root(world.root)
    rtl, tb = GCD_DESIGN
    design = Design(name="gcd", design_root=world.work, rtl=rtl, tb=tb)
    monkeypatch.chdir(world.work)
    _freeze(world.parent, keep=world.root)
    try:
        flow = DefaultRunner(world.root, display_results=False).launch_flow(
            "bsc_sim", design, {"vcd": "gcd.vcd"}
        )
    finally:
        _thaw(world.parent)
    assert flow.succeeded and (flow.run_path / "gcd.vcd").is_file()


#: A design generator: it writes the sources the design declares, and counts its runs in the one
#: place the oracle ignores, the run root. Its job is to write the design's own tree, so O1 admits
#: exactly the sources it generates -- and nothing else, xeda's own record of the generation
#: included, which lies under the run root (`xeda.generation`).
GENERATOR = """\
import os, sys
from pathlib import Path

root = Path(os.environ["DESIGN_ROOT"])
(root / "gen").mkdir(exist_ok=True)
(root / "gen" / "top.v").write_text("// generated\\n")
with open(sys.argv[1], "a") as counter:
    counter.write("ran\\n")
"""


def test_a_design_load_that_runs_a_generator_writes_only_the_sources_it_generates(tmp_path):
    """O1 and O3 for a generator: a load changes nothing outside the run root but the sources the
    design declares its generator produces. The generator writes the design's tree because that
    is what it is for; everything xeda keeps about it goes under the run root, and a second load
    of the unchanged design generates nothing at all."""
    world = _world(tmp_path)
    ensure_run_root(world.root)
    counter = world.root / "runs.log"
    (world.work / "gen.py").write_text(GENERATOR)
    (world.work / "spec.txt").write_text("one\n")
    design_file = world.work / "generated.yaml"
    design_file.write_text(
        "name: generated\n"
        "rtl:\n"
        "  sources: [gen/top.v]\n"
        "  top: top\n"
        "  generator:\n"
        f"    executable: {sys.executable!r}\n"
        f"    args: ['gen.py', {str(counter)!r}]\n"
        "    sources: ['spec.txt', 'gen.py']\n"
    )
    before = _state(world.parent, [world.root])
    with watching(world) as violations, loading_in_run_root(lambda create: world.root):
        design = Design.from_file(design_file)
        Design.from_file(design_file)
    assert [src.file.name for src in design.rtl.sources] == ["top.v"]
    assert counter.read_text() == "ran\n", "the second load generated again"
    assert violations == []
    after = _state(world.parent, [world.root])
    changed = {name for name in set(before) | set(after) if before.get(name) != after.get(name)}
    assert changed == {"work/gen", "work/gen/top.v"}
    records = sorted(p.name for p in (world.root / ".cache" / "generators").iterdir())
    assert len(records) == 2 and records[1] == records[0] + ".lock"


@pytest.mark.parametrize("flow, require", [("ghdl_sim", require_ghdl), ("nvc", require_nvc)])
def test_a_cocotb_simulation_leaves_the_design_directory_as_it_was(
    flow, require, tmp_path, monkeypatch
):
    """A real simulator runs sqrt's cocotb testbench from a copy of the example, with the
    default run root: the Python xeda sets up imports the testbench from the design's directory
    (pytest's assertion rewriting compiles it), and caches that bytecode in the run directory,
    never beside the testbench."""
    require()
    design_dir = tmp_path / "sqrt"
    design_dir.mkdir()
    for name in ("sqrt.yaml", "sqrt.vhdl", "tb_sqrt.py"):
        shutil.copy(SQRT / name, design_dir / name)
    monkeypatch.delenv("PYTHONDONTWRITEBYTECODE", raising=False)
    monkeypatch.delenv("PYTHONPYCACHEPREFIX", raising=False)
    monkeypatch.chdir(design_dir)
    root = design_dir / "xeda_run"
    before = _state(tmp_path, [root])
    result = CliRunner().invoke(cli, ["run", flow, "sqrt.yaml", "--json"])
    assert result.exit_code == 0, result.output
    assert _state(tmp_path, [root]) == before
    assert list(root.rglob("tb_sqrt.*.pyc")), "the testbench ran, its bytecode cached by xeda"


def test_the_command_line_changes_nothing_outside_the_run_root(tmp_path, monkeypatch):
    world = _world(tmp_path)
    shutil.copy(SQRT / "sqrt.yaml", world.work / "sqrt.yaml")
    before = _state(world.parent, [world.root])
    monkeypatch.setenv("PATH", str(FAKE_TOOLS_DIR) + os.pathsep + os.environ["PATH"])
    monkeypatch.chdir(world.work)
    args = ["run", "vivado_synth", "sqrt.yaml", "-s", "fpga.part=xc7a12tcsg325-1", "--json"]
    for extra in (["--clean"], ["--post-cleanup"], []):
        with watching(world) as violations:
            result = CliRunner().invoke(cli, [*args, *extra])
        assert result.exit_code == 0, result.output  # the oracle keeps its teeth
        assert not violations, violations
    assert _state(world.parent, [world.root]) == before


INV_TOML = """name = "inv"
[rtl]
sources = ["inv.v"]
top = "inv"
clock.port = "clk"
"""
INV_V = "module inv(input clk, input a, output reg y); always @(posedge clk) y <= ~a; endmodule\n"


@pytest.mark.parametrize("tools", ["fake xtclsh", "no xtclsh"])
def test_ise_synth_from_the_design_directory_deletes_none_of_its_files(
    tmp_path, monkeypatch, tools
):
    """The ise_synth probe (P10): `xeda run ise_synth inv.toml --cwd --clean`, started in the
    design's directory, deleted inv.toml, inv.v and an unrelated notes.txt. `--cwd` is gone; the
    run goes to ./xeda_run, and the directory keeps every file, with the tool or without it."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "inv.toml").write_text(INV_TOML)
    (work / "inv.v").write_text(INV_V)
    (work / "notes.txt").write_text("my notes\n")
    before = _state(work, [work / "xeda_run"])
    if tools == "fake xtclsh":
        use_fake_tools(monkeypatch)
    else:
        monkeypatch.setenv("PATH", os.defpath)
    monkeypatch.chdir(work)
    CliRunner().invoke(
        cli,
        [
            "run",
            "ise_synth",
            "inv.toml",
            "--clean",
            "-s",
            "fpga=xc6slx9-2-tqg144",
            "clock.period=10",
        ],
    )
    assert _state(work, [work / "xeda_run"]) == before, "the design directory changed"


# --- O4: the static scan (moved from tests/test_run_dir_ownership.py) --------------------------


def test_fake_tools_are_where_the_sweep_expects_them():
    for name in (
        "vivado",
        "xtclsh",
        "quartus_sh",
        "dc_shell",
        "diamondc",
        "vsim",
        "nextpnr-himbaechel",
        "nextpnr-ecp5",
        "nextpnr-ice40",
        "nextpnr-nexus",
        "fpga-as",
        "ecppack",
        "icepack",
        "openFPGALoader",
    ):
        assert (FAKE_TOOLS_DIR / name).exists()
    assert FAKED <= {cls.name for cls, _ in flow_classes()}


#: What deletes, moves over or overwrites a file or a tree, called by its name: a method
#: (`path.unlink()`), a module's function (`os.remove`, `shutil.rmtree`), or one imported by
#: name (`from os import remove`). `replace` with one argument is `Path.replace`; with two,
#: `str.replace` -- but `os.replace` always.
DELETING_CALLS = {
    "unlink",
    "remove",
    "rmdir",
    "removedirs",
    "rmtree",
    "rename",
    "renames",
    "move",
    "replace",
    "truncate",
}


def _python_deletions(package: Path) -> Counter:
    """Every call in xeda's Python code, but `xeda.run_dir` (the checked operation itself)
    and calls on a `run_directory`, that deletes, moves over or overwrites a file by name --
    by (module, source line), counted: a second copy of a reviewed line is a new site."""
    found: Counter = Counter()
    for path in sorted(package.rglob("*.py")):
        relative = path.relative_to(package).as_posix()
        if "__pycache__" in path.parts or relative == "run_dir.py":
            continue
        text = path.read_text()
        lines = text.splitlines()
        tree = ast.parse(text)
        imported = {  # names imported from os, shutil or pathlib that delete: `remove as rm`
            alias.asname or alias.name
            for node in ast.walk(tree)
            if isinstance(node, ast.ImportFrom) and node.module in ("os", "shutil", "pathlib")
            for alias in node.names
            if alias.name in DELETING_CALLS
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            if isinstance(function, ast.Attribute):
                receiver = ast.unparse(function.value)
                if function.attr not in DELETING_CALLS or receiver.endswith("run_directory"):
                    continue
                one_argument = len(node.args) == 1 and not node.keywords
                if function.attr == "replace" and receiver != "os" and not one_argument:
                    continue  # str.replace(old, new)
            elif not (isinstance(function, ast.Name) and function.id in imported):
                continue
            found[(relative, lines[node.lineno - 1].strip())] += 1
    return found


#: Every deletion in xeda's Python code outside `RunDirectory`, reviewed: what it removes, and
#: why that is only ever xeda's own. A new one, or a second copy of one, fails the oracle.
REVIEWED_PY_DELETIONS = {
    ("flows/xilinx.py", "temporary.remove(bba)"): (
        1,
        "RunDirectory.remove on the generator's own BBA in guarded cache scratch",
    ),
    ("flows/xilinx.py", "owner.remove(_cache_path(owner, interrupted))"): (
        1,
        "RunDirectory.remove of a killed generation's scratch for this key, under its "
        "exclusive entry lock, through the link-refusing cache path guard",
    ),
    ("flows/xilinx.py", "owner.remove(temporary)"): (
        1,
        "RunDirectory.remove on the managed cache's temporary entry after failure/publication",
    ),
    ("flows/xilinx.py", "temporary.rename(entry)"): (
        1,
        "publish a validated temporary entry under its identity lock, with source/destination "
        "checked against the marked run root and linked cache paths refused",
    ),
    ("digest.py", "marker.unlink(missing_ok=True)"): (1, "the clock marker it has just created"),
    ("flow_runner/trace.py", "(run_dir / TRACE_FILE).unlink(missing_ok=True)"): (1, "a trace"),
    ("flow_runner/trace.py", "temporary.unlink(missing_ok=True)"): (
        1,
        "a link or leftover at trace.json.tmp, xeda's reserved name, as itself",
    ),
    ("flow_runner/trace.py", "os.replace(temporary, path)"): (1, "trace.json.tmp over trace.json"),
    ("utils.py", "return path.rename(backup_path)"): (1, "a backup, to a name nothing has yet"),
    ("utils.py", "os.replace(temporary, target)"): (
        2,
        "`replacing_file`: its own complete temporary over the file it replaces (a failed tool's "
        "log too, with `keep_on_error`), which the caller located (`RunDirectory.writable`)",
    ),
    (
        "utils.py",
        "Path(temporary).unlink(missing_ok=True)  # the temporary file, never committed",
    ): (1, "`replacing_file`'s own temporary file, which `mkstemp` created"),
    (
        "flow_runner/default_runner.py",
        "p.unlink()  # the link itself, whose target, xeda's, is gone",
    ): (
        1,
        "`scrub_runs`: a run directory reached through a link in the run root, the link itself, "
        "once its target (under the run root) was removed",
    ),
    ("flows/yosys/common.py", "flags.remove(flag)"): (1, "a list of flags"),
    ("proc_utils.py", "readable.remove(fd)"): (1, "a list of file descriptors"),
    ("deliver.py", "os.replace(temporary, destination)"): (
        1,
        "the delivery's own temporary file, renamed over the destination the rules allowed",
    ),
    (
        "deliver.py",
        "temporary.unlink(missing_ok=True)  # its own temporary file, never anything else",
    ): (
        2,
        "its own temporary file, after a failed copy or a failed re-check",
    ),
    ("deliver.py", "os.replace(temporary, self.record_path)"): (
        1,
        "the delivery record's temporary file, beside the run directory",
    ),
}


def test_nothing_deletes_but_through_the_run_directory():
    """A mechanical oracle over all of xeda's Python code, by its syntax tree: nothing deletes,
    moves over or overwrites a file or a tree but through `RunDirectory` (`xeda.run_dir`), or
    one of the sites reviewed in `REVIEWED_PY_DELETIONS` -- each counted, so a copy of one is a
    new site."""
    found = _python_deletions(PACKAGE)
    expected = Counter({site: count for site, (count, _why) in REVIEWED_PY_DELETIONS.items()})
    assert found == expected, "not reviewed, reviewed but gone, or not as often"


def test_the_oracle_sees_what_deletes(tmp_path):
    """The oracle's teeth: each way of deleting by name is seen, the second copy of a reviewed
    line too; `str.replace` and a list's `remove` of a reviewed line are not new."""
    package = tmp_path
    (package / "a.py").write_text(
        "import os, shutil\n"
        "from os import remove as rm\n"
        "from pathlib import Path\n"
        "p = Path('x')\n"
        "p.unlink()\n"
        "p.unlink()\n"
        "os.removedirs(p)\n"
        "os.replace(p, p)\n"
        "shutil.move(p, p)\n"
        "rm(p)\n"
        "p.replace(p)\n"
        "'text'.replace('t', 'x')\n"
        "self.run_directory.remove(p)\n"
    )
    found = _python_deletions(package)
    assert found == Counter(
        {
            ("a.py", "p.unlink()"): 2,
            ("a.py", "os.removedirs(p)"): 1,
            ("a.py", "os.replace(p, p)"): 1,
            ("a.py", "shutil.move(p, p)"): 1,
            ("a.py", "rm(p)"): 1,
            ("a.py", "p.replace(p)"): 1,
        }
    )


#: In a tool script (every file of xeda's that is not Python: templates, the OpenROAD scripts,
#: the platforms' scripts): a deletion (`file delete`, `file rename`, `rm`), or a tool command
#: told to replace or overwrite what is there (`-force`, `-overwrite`).
SCRIPT_DELETION = re.compile(r"\brm\b|\bfile\s+(delete|rename)\b|(?<![\w-])-(force|overwrite)\b")
#: a tool command a flow runs that deletes where it runs (`ghdl remove`)
TOOL_DELETION = re.compile(r"\.run\(\s*[\"'](--)?(remove|clean)[\"']")

_REPORT = "a report, output or checkpoint the tool writes under its own name in the flow's "
_REPORT += "reports/outputs/checkpoints directory"
_BITSTREAM = "the bitstream named by the flow's `bitstream` setting"
#: a command that replaces by name, or deletes where it runs: only ever in the flow's run directory
IN_THE_RUN_DIRECTORY = "inside the flow's run directory, which is xeda's (D21)"

#: Every such line, reviewed, with how many times it occurs: (file, line) -> (count, why it
#: removes or replaces only xeda's own, and -- for a command that replaces by name or deletes
#: where it runs -- the flow module and the text of the guard in it, which must be there).
REVIEWED_SCRIPT_DELETIONS: dict = {
    (
        "flows/ghdl/__init__.py",
        'self.ghdl.run("remove", *ss.get_flags(vhdl, "remove", backend=backend))',
    ): (1, IN_THE_RUN_DIRECTORY, None),
    (
        "flows/vivado/templates/vivado_synth.tcl",
        "create_project -part $fpga_part -force -verbose ${project_name}",
    ): (1, IN_THE_RUN_DIRECTORY, None),
    (
        "flows/vivado/templates/vivado_project.tcl",
        "create_project {% if settings.fpga and settings.fpga.part -%} -part "
        '{{settings.fpga.part|tcl_word}} {%- endif %} -force -verbose "$project_name"',
    ): (1, IN_THE_RUN_DIRECTORY, None),
    (
        "flows/quartus/templates/create_project.tcl",
        "project_new ${design_name} -overwrite",
    ): (1, IN_THE_RUN_DIRECTORY, None),
    (
        "flows/dc/templates/dc_script.tcl",
        "write_icc2_files -force -output $OUTPUTS_DIR/icc2_files",
    ): (1, IN_THE_RUN_DIRECTORY, None),
    ("flows/dc/templates/dc_script.tcl", "if { [catch {uniquify -force} -errorinfo err] } {"): (
        1,
        "uniquifies the design in memory: no file",
        None,
    ),
    (
        "flows/vivado/templates/vivado_alt_synth.tcl",
        "write_checkpoint -force ${checkpoints_dir}/post_synth",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/vivado_alt_synth.tcl",
        "write_checkpoint -force ${checkpoints_dir}/post_place",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/vivado_alt_synth.tcl",
        "write_checkpoint -force ${checkpoints_dir}/post_route",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/vivado_alt_synth.tcl",
        "report_utilization -hierarchical -force -file ${reports_dir}/post_synth/hierarchical_utilization.rpt",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/vivado_alt_synth.tcl",
        "report_utilization -hierarchical -force -file ${reports_dir}/post_place/hierarchical_utilization.rpt",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/vivado_alt_synth.tcl",
        "write_verilog -mode funcsim -force ${settings.outputs_dir}/impl_funcsim.v",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/vivado_alt_synth.tcl",
        "write_sdf -mode timesim -process_corner slow -force -file ${settings.outputs_dir}/impl_timesim.sdf",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/vivado_alt_synth.tcl",
        "write_verilog -mode timesim -sdf_anno false -force -file ${settings.outputs_dir}/impl_timesim.v",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/vivado_alt_synth.tcl",
        "##    write_vhdl    -mode funcsim -include_xilinx_libs -write_all_overrides -force -file "
        "${settings.outputs_dir}/impl_funcsim_xlib.vhd",
    ): (1, "a comment", None),
    (
        "flows/vivado/templates/vivado_alt_synth.tcl",
        "write_xdc -no_fixed_only -force ${settings.outputs_dir}/impl.xdc",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/vivado_alt_synth.tcl",
        "write_bitstream -force {{settings.bitstream|tcl_word}}",
    ): (1, _BITSTREAM, None),
    (
        "flows/vivado/templates/post_step_hook.tcl",
        "report_utilization -force -file [file join ${xeda_reports_dir} utilization.xml] -format xml",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/post_step_hook.tcl",
        "report_utilization -force -file [file join ${xeda_reports_dir} hierarchical_utilization.xml] "
        "-format xml -hierarchical",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/post_step_hook.tcl",
        "report_utilization -force -file [file join ${xeda_reports_dir} utilization.rpt]",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/post_step_hook.tcl",
        "report_utilization -force -file [file join ${xeda_reports_dir} hierarchical_utilization.rpt] "
        "-hierarchical_percentages -hierarchical",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/post_step_hook.tcl",
        "write_qor_suggestions -quiet -strategy_dir [file join ${xeda_reports_dir} "
        "strategy_suggestions] -force [file join ${xeda_reports_dir} qor_suggestions.rqs]",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/post_step_hook.tcl",
        "write_checkpoint -force {{outputs.checkpoint_synth|tcl_word}}",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/post_step_hook.tcl",
        "write_checkpoint -force {{outputs.checkpoint_route|tcl_word}}",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/post_step_hook.tcl",
        "write_verilog -mode funcsim -force -file {{outputs.netlist|tcl_word}}",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/post_step_hook.tcl",
        "write_verilog -mode timesim -sdf_anno false -force -file "
        "{{outputs.netlist_timing|tcl_word}}",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/post_step_hook.tcl",
        "write_sdf -mode timesim -process_corner slow -force -file {{outputs.sdf_max|tcl_word}}",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/post_step_hook.tcl",
        "write_sdf -mode timesim -process_corner fast -force -file {{outputs.sdf_min|tcl_word}}",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/post_step_hook.tcl",
        "write_xdc -no_fixed_only -force {{outputs.xdc_exported|tcl_word}}",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/write_bitstream_hook.tcl",
        "file copy -force ${xeda_top}.bit {{outputs.bitstream|tcl_word}}",
    ): (1, _BITSTREAM + ", copied from the implementation run's own", None),
    (
        "flows/vivado/templates/write_bitstream_hook.tcl",
        "file copy -force ${xeda_top}.bin {{bin_file|tcl_word}}",
    ): (1, "the .bin beside the bitstream, copied from the implementation run's own", None),
    ("platforms/nangate45/fakeram.tcl", "file delete fakeram45_$size.lib"): (
        1,
        "an upstream " "PDK helper script xeda never runs",
        None,
    ),
    ("platforms/nangate45/fakeram.tcl", "file delete fakeram45_$size.lef"): (
        1,
        "an upstream " "PDK helper script xeda never runs",
        None,
    ),
    (
        "platforms/nangate45/fakeram.tcl",
        "file copy -force $results_dir/fakeram45_$size/fakeram45_$size.lib $flow_dir/lib/fakeram45_$size.lib",
    ): (1, "an upstream PDK helper script xeda never runs", None),
    (
        "platforms/nangate45/fakeram.tcl",
        "file copy -force $results_dir/fakeram45_$size/fakeram45_$size.lef $flow_dir/lef/fakeram45_$size.lef",
    ): (1, "an upstream PDK helper script xeda never runs", None),
    (
        "flows/diamond/templates/synth.tcl",
        "prj_project new -name {{design.name|tcl_word}} -dev {{settings.fpga.part|tcl_word}} "
        "-impl $implementation_name -impl_dir $impl_dir",
    ): (1, IN_THE_RUN_DIRECTORY, None),
    (
        "flows/ise/templates/ise_synth.tcl",
        "if { [catch  { project new {{design.name|tcl_word}} }] } {",
    ): (
        1,
        IN_THE_RUN_DIRECTORY,
        None,
    ),
}

#: Commands that replace a project or a directory by its name, whatever their options: each is
#: reviewed above.
SCRIPT_REPLACEMENT = re.compile(
    r"\bcreate_project\b|\bproject_new\b|\bprj_project\s+new\b|\bproject\s+new\b|"
    r"\bwrite_icc2_files\b"
)


def test_every_tool_command_that_deletes_or_replaces_is_reviewed_and_guarded():
    """A mechanical oracle over every file of xeda's that is not Python -- tool scripts, the
    OpenROAD and platform scripts -- and every tool command in a flow's code: each line that
    deletes (`file delete`, `file rename`, `rm`), tells a tool to replace or overwrite
    (`-force`, `-overwrite`), replaces by name (`create_project`, `project_new`, ...) or runs a
    deleting tool command (`ghdl remove`) is reviewed in `REVIEWED_SCRIPT_DELETIONS`, as often as
    it occurs; one that replaces by name or deletes where it runs has its guard in its flow, if
    it needs one."""
    package = PACKAGE
    found: Counter = Counter()
    for path in sorted(package.rglob("*")):
        if not path.is_file() or "__pycache__" in path.parts or path.suffix in (".pyc", ".gz"):
            continue
        pattern = TOOL_DELETION if path.suffix == ".py" else None
        for line in path.read_text(errors="ignore").splitlines():
            text = line.strip()
            if pattern is not None:
                hit = pattern.search(text) and not text.startswith("#")
            else:
                hit = SCRIPT_DELETION.search(text) or SCRIPT_REPLACEMENT.search(text)
            if hit:
                found[(path.relative_to(package).as_posix(), text)] += 1
    expected = Counter(
        {site: count for site, (count, _why, _guard) in REVIEWED_SCRIPT_DELETIONS.items()}
    )
    assert found == expected, "not reviewed, reviewed but gone, or not as often"
    for _count, _why, guard in REVIEWED_SCRIPT_DELETIONS.values():
        if guard is not None:
            module, text = guard
            assert text in (package / module).read_text(), f"{module} lost its guard {text!r}"


# ---------------------------------------------------------------------------------------------
# O4, the static scan, for writes: every file xeda writes by name goes through
# `utils.replacing_file`/`replacing_copy` (complete-then-rename, which replaces a link at the name
# rather than writing through it) -- in a run directory, at the path `RunDirectory.writable`
# located -- or is one reviewed here.


def _write_imports(tree: ast.AST) -> tuple[dict[str, str], dict[str, tuple[str, str]]]:
    """The module aliases (`import os as o`: `o` -> `os`) and the imported names (`from os
    import remove as r`: `r` -> (`os`, `remove`)) of a module."""
    modules: dict[str, str] = {}
    names: dict[str, tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules[alias.asname or alias.name.split(".")[0]] = alias.name.split(".")[0]
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                names[alias.asname or alias.name] = (node.module.split(".")[0], alias.name)
    return modules, names


#: Module functions and methods that write, create, copy onto or rename onto a path.
MODULE_WRITES = {("shutil", f) for f in ("copy", "copy2", "copyfile", "copytree", "move")}
MODULE_WRITES |= {("os", f) for f in ("rename", "renames", "replace", "open", "symlink", "link")}
WRITING_METHODS = {"write_text", "write_bytes", "symlink_to", "hardlink_to", "touch", "rename"}
#: Modules whose `open(path, mode)` takes the mode second (`Path.open` takes it first).
OPENING_MODULES = {"gzip", "bz2", "lzma", "io", "codecs", "os"}


def _is_mode(node: ast.expr) -> bool:
    return (
        isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and bool(node.value)
        and set(node.value) <= set("rwaxbt+")
    )


def _write_mode(node: ast.Call, position: int) -> bool:
    """Whether the mode of an `open` call writes -- or is not a constant, and so may. The mode
    is its `mode=`, else the argument at `position`, else any other argument that spells a mode
    (`sftp.open(path, "r")`)."""
    mode = next((k.value for k in node.keywords if k.arg == "mode"), None)
    if mode is None and len(node.args) > position:
        mode = node.args[position]
    if mode is not None and not _is_mode(mode):
        mode = next((arg for arg in node.args if _is_mode(arg)), mode)
    if mode is None:
        return False
    choices = [mode.body, mode.orelse] if isinstance(mode, ast.IfExp) else [mode]
    return any(
        not _is_mode(choice) or bool(set(choice.value) & set("wax+"))  # type: ignore[attr-defined]
        for choice in choices
    )


def write_sites(text: str) -> Iterator[str]:
    """The stripped source line of each place Python `text` writes a file by name with a raw
    primitive -- `open` for writing, `write_text`, a copy, a rename, a link -- rather than
    through `utils.replacing_file`/`replacing_copy`, which never write through a link."""
    tree = ast.parse(text)
    lines = text.splitlines()
    modules, names = _write_imports(tree)
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        writes = False
        if isinstance(func, ast.Name):
            module, name = names.get(func.id, ("", func.id))
            writes = (module, name) in MODULE_WRITES or (name == "open" and _write_mode(node, 1))
        elif isinstance(func, ast.Attribute):
            receiver = func.value.id if isinstance(func.value, ast.Name) else ""
            module = modules.get(receiver, receiver)
            if (module, func.attr) in MODULE_WRITES or func.attr in WRITING_METHODS:
                writes = True
            elif func.attr in ("open", "fdopen"):
                writes = _write_mode(node, 1 if module in OPENING_MODULES else 0)
            elif func.attr == "replace":  # `Path.replace(target)`, never `str.replace(a, b)`
                writes = (len(node.args) == 1 and not node.keywords) or any(
                    k.arg == "target" for k in node.keywords
                )
        if writes:
            yield lines[node.lineno - 1].strip()


_OUTSIDE_RUNS = "not a run directory's"

#: Every raw write by name, why it cannot write through a link into a file of the user's:
#: `(file, line, reason)`. Everything else xeda writes goes through `replacing_file`.
REVIEWED_WRITES = [
    (
        "flows/xilinx.py",
        "temporary.rename(entry)",
        "a complete validated chipdb entry, in guarded managed cache space under the marked "
        "run root; published under its durable identity lock, never through linked parents",
    ),
    ("utils.py", "os.replace(temporary, target)", "`replacing_file`: its complete temporary"),
    (
        "utils.py",
        "os.replace(temporary, target)",
        "`replacing_file(keep_on_error=True)`: a failed tool's redirected output, kept",
    ),
    (
        "utils.py",
        'with os.fdopen(fd, mode, encoding=None if "b" in mode else encoding) as f:',
        "`replacing_file`: the temporary file `mkstemp` just created",
    ),
    ("utils.py", "return path.rename(backup_path)", "`backup_existing`: a rename, not a write"),
    (
        "deliver.py",
        "os.replace(temporary, destination)",
        "a delivery: its complete temporary beside the destination, renamed over it (a link at "
        "the name is replaced, never followed)",
    ),
    (
        "deliver.py",
        "os.replace(temporary, self.record_path)",
        "the delivery record: its complete temporary, renamed over it",
    ),
    ("deliver.py", "temporary.write_text(", "the delivery record's temporary, a new file"),
    (
        "deliver.py",
        'with os.fdopen(fd, "wb") as out, open(source, "rb") as data:',
        "a delivery's temporary, which `mkstemp` just created beside the destination",
    ),
    (
        "flow_runner/run_lock.py",
        'with open(path, "a") as f:',
        "the lock file beside a run directory, in the run root: opened to be locked, not written",
    ),
    (
        "flow_runner/trace.py",
        "fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)",
        "the trace's temporary, created exclusively (an existing entry is refused)",
    ),
    ("flow_runner/trace.py", "os.replace(temporary, path)", "the trace: its complete temporary"),
    ("flow_runner/trace.py", 'with os.fdopen(fd, "w") as f:', "the trace's temporary, just made"),
    (
        "run_root.py",
        "fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)",
        "a run root's marker or ignore file, created exclusively: an existing entry is kept",
    ),
    ("run_root.py", 'with os.fdopen(fd, "w") as f:', "the marker or ignore file it just created"),
    (
        "flow_runner/remote.py",
        'with open(design_file, "w") as f:',
        f"`send_design`: the archive's design file, in a `TemporaryDirectory` it made: {_OUTSIDE_RUNS}",
    ),
    (
        "flows/yosys/common.py",
        "alias.symlink_to(target, target_is_directory=target.is_dir())",
        "creates a link of its own under `path_aliases/`, where no entry is left at the name",
    ),
    (
        "agent_skill.py",
        'flows_md.write_text(generate_flows_reference(), encoding="utf-8")',
        f"`xeda skill install` into the directory the user names: {_OUTSIDE_RUNS}",
    ),
    (
        "agent_skill.py",
        'shutil.copyfile(reference, target / "references" / reference.name)',
        f"`xeda skill install`: {_OUTSIDE_RUNS}",
    ),
    (
        "agent_skill.py",
        'shutil.copyfile(source / "SKILL.md", target / "SKILL.md")',
        f"`xeda skill install`: {_OUTSIDE_RUNS}",
    ),
    (
        "platforms/asap7/openroad/post_mergeLib.py",
        'fo = open(mergedFile, "w")',
        "a PDK script the tool runs, not xeda",
    ),
    (
        "platforms/mk_to_toml.py",
        'with open(Path(args.config_mk).parent / "config.toml", "w") as f:',
        f"a developer's script converting a PDK's config: {_OUTSIDE_RUNS}",
    ),
]


def test_every_raw_write_by_name_is_reviewed():
    """A mechanical oracle: every place in `src/xeda` that writes a file by name with a raw
    primitive, rather than through `replacing_file`/`replacing_copy` (which replace a link at the
    name instead of writing through it), is one reviewed in `REVIEWED_WRITES`, as many times as it
    occurs."""
    found: Counter = Counter()
    for path in sorted(PACKAGE.rglob("*.py")):
        if "__pycache__" not in path.parts:
            for line in write_sites(path.read_text()):
                found[(path.relative_to(PACKAGE).as_posix(), line)] += 1
    reviewed = Counter((file, line) for file, line, _ in REVIEWED_WRITES)
    assert sorted((found - reviewed).elements()) == [], "not reviewed"
    assert sorted((reviewed - found).elements()) == [], "reviewed, but no longer there"


WRITE_MUTATIONS = [
    'def f(p):\n    open(p, "w")\n',
    'def f(p):\n    open(p, mode="a")\n',
    "def f(p, m):\n    open(p, m)\n",
    'def f(p):\n    p.open("w")\n',
    'def f(p):\n    p.write_text("x")\n',
    "import shutil\n\ndef f(a, b):\n    shutil.copy(a, b)\n",
    "from shutil import copyfile as c\n\ndef f(a, b):\n    c(a, b)\n",
    "import os as o\n\ndef f(a, b):\n    o.replace(a, b)\n",
    "def f(p, t):\n    p.replace(t)\n",
]


@pytest.mark.parametrize("added", WRITE_MUTATIONS)
def test_the_write_oracle_notices_every_spelling_of_a_write(added):
    """Each raw write appended to a scratch copy of a flow's text is a site the oracle notices;
    a read and `str.replace` are not."""
    text = (PACKAGE / "flows" / "ghdl" / "__init__.py").read_text()
    assert Counter(write_sites(text + "\n" + added)) - Counter(write_sites(text)), added
    assert not list(write_sites('open("a")\nopen("a", "rb")\n"x".replace("x", "y")\n'))


# ---------------------------------------------------------------------------------------------
# Symbolic links a tool leaves in its run directory: allowed, never followed out of it.


def _outside_tree(root: Path) -> dict:
    """Every entry under `root`, as a canary: its kind and content (or link text)."""
    return {
        str(p.relative_to(root)): p.read_text() if p.is_file() else "dir"
        for p in sorted(root.rglob("*"))
    }


def test_links_a_tool_left_are_never_followed_out_of_the_run_directory(tmp_path, monkeypatch):
    """A tool may leave symbolic links in its run directory -- to files and directories inside
    it and out of it, dangling, in a cycle, and at the name of a working location. A launch in
    that directory again, `--clean`, post-cleanup, a purge and `xeda scrub` all succeed and leave
    what the links lead to outside unchanged; a link at a working location's name that leads out
    is removed as a link before the run, never what it leads to; and a delivery never expands a
    link to a directory outside, nor copies from a link that leads nowhere -- each is a
    `DeliveryError` naming it -- while a link to a file delivers that file's content."""
    from xeda.dataclass import WORKING, Field
    from xeda.deliver import DeliveryError
    from xeda.flow import Flow, registered_flows
    from xeda.flow_runner.default_runner import scrub_runs

    outside = tmp_path / "install"
    (outside / "bin").mkdir(parents=True)
    (outside / "bin" / "tool").write_text("a tool\n")
    (outside / "canary.txt").write_text("outside\n")
    before = _outside_tree(outside)
    deliver: dict = {}

    class LinkLeaver(Flow):
        """Leaves every kind of link in its run directory, as a tool may."""

        results_description: dict = {}

        class Settings(Flow.Settings):
            work_dir: Path = Field(Path("work"), description="its work.", json_schema_extra=WORKING)

        def run(self) -> None:
            run_dir = Path.cwd()
            (run_dir / "work").mkdir(exist_ok=True)
            (run_dir / "work" / "made.txt").write_text("made\n")
            (run_dir / "inside.txt").write_text("inside\n")
            (run_dir / "sub").mkdir(exist_ok=True)
            (run_dir / "sub" / "f.txt").write_text("in sub\n")
            links = {
                "link_in_file": "inside.txt",
                "link_out_file": str(outside / "canary.txt"),
                "link_out_dir": str(outside),
                "link_in_dir": "sub",
                "dangling": "nowhere",
                "cycle_a": "cycle_b",
                "cycle_b": "cycle_a",
            }
            for name, target in links.items():
                if not os.path.lexists(run_dir / name):
                    (run_dir / name).symlink_to(target)
            self.artifacts.update(deliver)

    design = Design(name="links", design_root=tmp_path, rtl={"sources": [str(SQRT / "sqrt.vhdl")]})
    root = tmp_path / "xeda_run"
    try:
        flow = DefaultRunner(root, display_results=False).launch_flow(LinkLeaver, design, {})
        run_dir = flow.run_path
        assert flow.succeeded
        # a tool turned the working location into a link out: removed as a link, then made anew
        (run_dir / "work").rename(run_dir / "work_old")
        (run_dir / "work").symlink_to(outside, target_is_directory=True)
        for launcher in ({"rebuild_all": True}, {"clean": True}, {"post_cleanup": True}):
            again = DefaultRunner(root, display_results=False, **launcher)
            assert again.launch_flow(LinkLeaver, design, {}).succeeded, launcher
            assert not (run_dir / "work").is_symlink(), launcher
            assert _outside_tree(outside) == before, launcher
        # deliveries: a link to a file delivers its content; a directory link inside, its tree
        delivered = tmp_path / "delivered"
        deliver.update(a="link_in_file", b="link_out_file", c="link_in_dir")
        runner = DefaultRunner(root, display_results=False, rebuild_all=True, outputs_to=delivered)
        assert runner.launch_flow(LinkLeaver, design, {}).succeeded
        assert (delivered / "link_in_file").read_text() == "inside\n"
        assert (delivered / "link_out_file").read_text() == "outside\n"
        assert (delivered / "link_in_dir" / "f.txt").read_text() == "in sub\n"
        for artifact in ("link_out_dir", "dangling", "cycle_a"):
            deliver.clear()
            deliver["x"] = artifact
            runner = DefaultRunner(
                root, display_results=False, rebuild_all=True, outputs_to=tmp_path / artifact
            )
            with pytest.raises(DeliveryError, match=re.escape(str(run_dir / artifact))):
                runner.launch_flow(LinkLeaver, design, {})
            assert not (tmp_path / artifact / "bin").exists()
        deliver.clear()
        assert _outside_tree(outside) == before
        # purge, then scrub: the run directory goes, the links as links
        purge = DefaultRunner(
            root,
            display_results=False,
            rebuild_all=True,
            post_cleanup=True,
            post_cleanup_purge=True,
        )
        assert purge.launch_flow(LinkLeaver, design, {}).succeeded
        assert not run_dir.exists()
        assert DefaultRunner(root, display_results=False).launch_flow(LinkLeaver, design, {})
        monkeypatch.setattr("xeda.flow_runner.default_runner.console.input", lambda _: "yes")
        assert scrub_runs(LinkLeaver.name, run_dir.parent, run_root=root.resolve())
        assert not run_dir.exists()
        assert _outside_tree(outside) == before
    finally:
        for name in (LinkLeaver.name, LinkLeaver.__name__):
            registered_flows.pop(name, None)
