"""Outside the run roots it created, xeda changes nothing but the output paths a launch named:
the isolation oracle. Everything runs in scratch copies under `tmp_path`.

The same launches carry the oracle of the process (`test_shared_state.py` holds its teeth): a
launch leaves nothing of itself that a later launch could meet. The launches of a case leave the
tables, defaults and class attributes of `xeda.*` as they were (`state_snapshot.state`), share no
mutable object with them (`state_snapshot.shared_objects`), and leave the working directory, the
environment, `sys.path`, the logging and the warning filters as they were. They also leave the
settings mapping and the `Design` that the caller gave as they were.

The audit hook also fails a launch that changes the working directory, opens, lists or writes a
relative path, or starts a process without an absolute `cwd=`. A relative `stat` (`exists`,
`resolve`) raises no audit event. The sweeps do not change into the design's directory, so such a
use reads the checkout, not the run directory, and the launch fails or reads the wrong file.

What the oracle cannot see:
- A tool outside `FAKED` is stubbed (`run_process` does nothing), so for those flows only xeda's
  own writes and the fake TCL tools' are observed, never what the real tool would write.
- The audit hook sees this process only, not the tools it starts; the snapshot and the read-only
  tree see those through the file system.
- Only the start directory's parent (`world.parent`) is snapshotted, made read-only and audited:
  an absolute write elsewhere, the real home directory for one, is seen by nothing.
- A process that a tool's own children start is not seen. The processes the sweep records are the
  ones that this interpreter asks for (`ProcessRecorder`); it judges where they start, not what
  they read or write after that.
- A read that depends on the directory of the caller is not detected, because the sweep launches
  from one directory only.
The opt-in real-tool layers (`XEDA_TESTS_VIVADO`, `XEDA_TESTS_DOCKER`, `XEDA_TESTS_EXTERNAL`) are
where the sweep can be extended to real tools (a follow-up); real GHDL, nvc and bsc already run in
`test_a_cocotb_simulation_leaves_the_design_directory_as_it_was` and
`test_bsc_sim_simulates_the_bluespec_example_on_a_read_only_tree`."""

import ast
import contextlib
import copy
import enum
import inspect
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import typing
from collections import Counter
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, NamedTuple, Optional, Sequence, Tuple

import pytest
import yaml
from click.testing import CliRunner

import xeda
import xeda.tool as xeda_tool
from xeda.flow import MetricSpec
from xeda import Design
from xeda.cli import cli
from xeda.flow import Flow, SimFlow
from xeda.design import SourceType
from xeda.flow_runner import DefaultRunner
from xeda.proc_utils import run_process
from xeda.run_root import ensure_run_root

from . import state_snapshot as ss
from .settings_samples import flow_classes, minimal_settings
from .tiny_platform import TINY_OPENROAD_RESIDUE, TINY_RESIDUE, tiny_settings, tiny_store
from .tool_utils import (
    FAKE_TOOLS_DIR,
    require_bluesim,
    require_bsc,
    require_ghdl,
    require_nvc,
    require_yosys,
    use_fake_tools,
)
from .scoping import node_settings

pytestmark = pytest.mark.python_compat


SQRT = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt"
#: The Bluespec example, for the flows whose tools read Bluespec
GCD = Path(__file__).parent.parent / "examples" / "bluespec" / "gcd"
PACKAGE = Path(xeda.__file__).parent

#: Where the stand-in FPGA tools are installed, in the run root
FAKE_TOOLCHAIN = ".fake-toolchain"
#: FPGA flows use process fakes, including an opt-in fake synthesis executable.
FPGA_FAKED = {"yosys_fpga", "nextpnr", "fpga_pack", "openfpgaloader", "vivado_impl"}
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
    "openroad",
    "openroad_cts",
    "openroad_grt",
    "openroad_place",
} | FPGA_FAKED
#: the design most flows run: sqrt, with its cocotb testbench
SQRT_DESIGN = (
    {"sources": ["sqrt.vhdl"], "top": "sqrt", "clocks": [{"port": "clk"}]},
    {"sources": ["tb_sqrt.py"], "cocotb": True},
)
#: `gcd` for the flows whose tools read Bluespec: `bsc_sim` simulates its Bluespec testbench
#: (it rejects a cocotb one before `run()`)
GCD_DESIGN = (
    {
        "sources": ["rtl/GcdEngine.bsv", "rtl/GcdUnit.bsv"],
        "top": "mkGcdUnit",
        "clocks": [{"port": "CLK"}],
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
#: sqrt's cocotb testbench on a Verilog stand-in for the unit (`SQRT_VERILOG`), for the cocotb
#: simulators that do not read VHDL (they refuse a design with a VHDL source at launch)
SQRT_VERILOG = "module sqrt_stand_in(input clk); endmodule\n"
SQRT_VERILOG_DESIGN = (
    {"sources": ["sqrt_stand_in.v"], "top": "sqrt_stand_in", "clocks": [{"port": "clk"}]},
    SQRT_DESIGN[1],
)
#: a gate-level netlist for `openroad_place`, which reads one and runs no synthesis; the flows
#: after it start from its database
SQRT_NETLIST = "module sqrt_stand_in(input clk); endmodule\n"
SQRT_NETLIST_DESIGN = (
    {
        "sources": [{"file": "sqrt_stand_in_netlist.v", "type": "VerilogNetlist"}],
        "top": "sqrt_stand_in",
        "clocks": [{"port": "clk"}],
    },
    SQRT_DESIGN[1],
)
DESIGNS = {
    "bsc": GCD_DESIGN,
    "bsc_sim": GCD_DESIGN,
    "openroad": SQRT_NETLIST_DESIGN,
    "openroad_cts": SQRT_NETLIST_DESIGN,
    "openroad_grt": SQRT_NETLIST_DESIGN,
    "openroad_place": SQRT_NETLIST_DESIGN,
}
DESIGNS.update(
    (cls.name, SQRT_VERILOG_DESIGN)
    for cls, _ in flow_classes()
    if issubclass(cls, SimFlow)
    and cls.cocotb_sim_name
    and cls.reads_sources is not None
    and SourceType.Vhdl not in cls.reads_sources
    and cls.name not in DESIGNS
)
DESIGNS.update(
    (cls.name, PLAIN_TB_DESIGN)
    for cls, _ in flow_classes()
    if issubclass(cls, SimFlow) and not cls.cocotb_sim_name and cls.name not in DESIGNS
)
#: `vivado_sim` records the activity of the instance `tb.uut`, and refuses a design without it
DESIGNS["vivado_sim"] = (PLAIN_TB_DESIGN[0], {**PLAIN_TB_DESIGN[1], "uut": "uut"})
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
    "nvc": {"fst": "dump.fst"},
    "verilator": {"vcd": "dump.vcd"},
    "yosys_fpga": {"netlist_json": "net.json", "netlist_edif": "net.edif"},
    "vivado_impl": {"bitstream": "top.bit"},
    "yosys": {"netlist_verilog": "net.v"},
    "nextpnr": {"textcfg": "cfg.txt"},
    "fpga_pack": {"bitstream": "top.bit"},
    "ghdl_synth": {"verilog_output": "sqrt.v"},
    "bsc_sim": {"vcd": "bsc_sim.vcd"},
}
#: The flows that read the values of OpenROAD from the platform
OPENROAD_FLOWS = {"openroad", "openroad_cts", "openroad_grt", "openroad_place"}
#: The flows the resolver refuses in the sweep's world, with what its refusal says: the sweep's
#: testbench is cocotb's, and `vivado_postsynth_sim`, which `vivado_power` reads, cannot run it.
UNPLANNABLE = {
    "vivado_power": "vivado_postsynth_sim cannot run cocotb tests",
}
#: The flows the sweep does not bring to their `run()`, and why nothing is lost.
UNREACHED = {
    **{name: f"its plan is refused ({reason})" for name, reason in UNPLANNABLE.items()},
}

#: The flows whose launches ask for no process but a probe, with the reason. The sweep then
#: sees no process of theirs, so the check of the directory of a process says nothing of them.
NO_PROCESS_IN_RUN: Dict[str, str] = {
    "bsc_sim": "its Verilator link needs `VERILATOR_ROOT` from `verilator -V`, which the "
    "stand-in tool does not print: `run()` ends in a settings error before it starts a tool",
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
    pdk_store: Path  # the store of the `tiny` platform, for a flow that maps to one


#: The share directory of the stand-in cocotb, in the world's parent: Verilator's flow copies
#: cocotb's main for Verilator from it (`lib/verilator/verilator.cpp`).
COCOTB_SHARE = "cocotb_share"


def stand_in_answers(world: World) -> Dict[Tuple[str, str], str]:
    """What the stand-in of `run_process` prints for a query, by program name and first argument,
    so that a flow that needs the answer reaches the tools it starts."""
    return {
        ("cocotb-config", "--share"): str(world.parent / COCOTB_SHARE),
        ("cocotb-config", "--python-bin"): sys.executable,
    }


def _world(parent: Path) -> World:
    work, outside = parent / "work", parent / "outside"
    work.mkdir(parents=True)
    outside.mkdir()
    cocotb_main = parent / COCOTB_SHARE / "lib" / "verilator" / "verilator.cpp"
    cocotb_main.parent.mkdir(parents=True)
    cocotb_main.write_text("// the stand-in cocotb's main for Verilator\n")
    for name in ("sqrt.vhdl", "tb_sqrt.py"):
        shutil.copy(SQRT / name, work / name)
    (work / "tb_sqrt_plain.vhdl").write_text(PLAIN_TB)
    (work / "sqrt_stand_in.v").write_text(SQRT_VERILOG)
    (work / "sqrt_stand_in_netlist.v").write_text(SQRT_NETLIST)
    for part in ("rtl", "tb"):
        shutil.copytree(GCD / part, work / part)
    for name in CANARIES:
        (work / name).parent.mkdir(parents=True, exist_ok=True)
        (work / name).write_text(f"the user's {name}\n")
    (outside / "target.txt").write_text("outside\n")
    (work / "link_to_outside").symlink_to(outside / "target.txt")
    return World(
        parent, work, outside, work / "xeda_run", work / "delivered", tiny_store(parent, "pdks")
    )


def sweep_settings(flow_class, world: World) -> dict:
    """The settings the sweeps launch `flow_class` with in `world`: the least it needs, what makes
    it reach what it writes, and for a flow that maps to a platform, the platform whose files the
    world holds."""
    settings = {**minimal_settings(flow_class), **EXTRA_SETTINGS.get(flow_class.name, {})}
    if "platform" in settings:
        residue = TINY_OPENROAD_RESIDUE if flow_class.name in OPENROAD_FLOWS else TINY_RESIDUE
        settings.update(tiny_settings(world.pdk_store, residue=residue), platform="tiny")
    return settings


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


# --- the audit hook -------------------------------------------------------------------------

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


#: The events that list a directory
_READS_PATH = ("os.listdir", "os.scandir")
#: The events that create, change or remove a path, and the positions of their paths in the
#: arguments (`os.rename`, `os.link` and the copies name two)
_PATH_EVENTS = {
    "os.mkdir": (0,),
    "os.remove": (0,),
    "os.rmdir": (0,),
    "os.chmod": (0,),
    "os.utime": (0,),
    "os.truncate": (0,),
    "os.rename": (0, 1),
    "os.symlink": (1,),  # the new name; the first argument is the link's text
    "os.link": (0, 1),
    "shutil.copyfile": (0, 1),
    "shutil.rmtree": (0,),
    "shutil.move": (0, 1),
    "shutil.copytree": (0, 1),
}
#: The positions of the directory descriptors of an event, which a path may be relative to
_DIR_FD = {
    "os.mkdir": (2,),
    "os.remove": (1,),
    "os.rmdir": (1,),
    "os.chmod": (2,),
    "os.utime": (3,),
    "os.rename": (2, 3),
    "os.symlink": (2,),
    "os.link": (2, 3),
    "shutil.rmtree": (1,),
}
#: Modules that open a path relative to a directory descriptor (`shutil.rmtree` and `os.fwalk`
#: walk a tree that way); the `open` event does not carry the descriptor.
_DIR_FD_OPENERS = (shutil.__file__, os.__file__, "<frozen os>")
_IN_HOOK = threading.local()


def _relative(raw: object) -> bool:
    """Whether an audited path is resolved against the working directory. A file descriptor is
    opened already; `None` is the working directory itself (`os.listdir()`)."""
    if isinstance(raw, int):
        return False
    if raw is None:
        return True
    return isinstance(raw, (str, bytes, os.PathLike)) and not os.path.isabs(os.fsdecode(raw))


def _relative_paths(event: str, args: tuple) -> List[object]:
    """The paths of an event that resolve against the working directory: each path is relative
    and has no directory descriptor of its own (`_DIR_FD` pairs them in order)."""
    descriptors = _DIR_FD.get(event, ())
    found = []
    for k, i in enumerate(_PATH_EVENTS[event]):
        fd = args[descriptors[k]] if k < len(descriptors) and descriptors[k] < len(args) else None
        if fd in (None, -1) and _relative(args[i]):
            found.append(args[i])
    return found


def _relative_uses(event: str, args: tuple) -> List[str]:
    """The paths an audited event resolves against the working directory, and the processes it
    starts there: each as the text of a violation."""
    if event in ("os.chdir", "os.fchdir"):
        return [f"{event} {args[0]}"]
    if event == "subprocess.Popen":
        cwd = args[2]
        return [f"process without an absolute cwd {args[1]}"] if _relative(cwd) else []
    if event == "glob.glob/2":  # (pattern, recursive, root_dir, dir_fd)
        _, _, root_dir, dir_fd = args
        by_root = dir_fd is not None or (root_dir is not None and not _relative(root_dir))
        return [] if by_root or not _relative(args[0]) else [f"relative {event} {args[0]}"]
    if event == "open":
        by_descriptor = sys._getframe(2).f_code.co_filename in _DIR_FD_OPENERS
        return [f"relative open {args[0]}"] if _relative(args[0]) and not by_descriptor else []
    if event in _READS_PATH:
        return [f"relative {event} {args[0]}"] if _relative(args[0]) else []
    if event == "os.fwalk":  # (top, topdown, onerror, follow_symlinks, dir_fd)
        return [f"relative {event} {args[0]}"] if args[4] is None and _relative(args[0]) else []
    if event in _PATH_EVENTS:
        return [f"relative {event} {path}" for path in _relative_paths(event, args)]
    return []


def _audit(event: str, args: tuple) -> None:
    if not _WATCH or getattr(_IN_HOOK, "busy", False):
        return
    _IN_HOOK.busy = True
    try:
        _WATCH["violations"].extend(_relative_uses(event, args))
        _audit_writes(event, args)
    finally:
        _IN_HOOK.busy = False


def _audit_writes(event: str, args: tuple) -> None:
    world: World = _WATCH["world"]
    for raw in _written(event, args):
        if not isinstance(raw, (str, bytes, os.PathLike)):
            continue  # a file descriptor: opened already, by path
        path = _resolved(raw)
        if not path.is_relative_to(world.parent) or path.is_relative_to(world.root):
            continue
        if path.is_relative_to(world.delivered) and _from_deliver():
            if event == "os.rename" and raw is args[1]:  # a delivery, renamed into place
                # a file moved out of the run directory first arrives under the delivery's
                # temporary name, which is no delivery: a temporary left behind is a change
                staged = path.name.startswith(".xeda-delivery-")
                _WATCH["moved" if staged else "delivered"].append(path)
            continue
        _WATCH["violations"].append(f"{event} {path}")


sys.addaudithook(_audit)


@contextmanager
def watching(
    world: World, delivered: Optional[list] = None, moved: Optional[list] = None
) -> Iterator[list]:
    """Audit the launches in the block: yields the violations; `delivered` collects every file
    `xeda/deliver.py` renamed into place, and `moved` each file it moved out of a run directory
    to a delivery's temporary name (the first of the two renames of a delivery by move)."""
    violations: list = []
    _WATCH.update(
        world=world,
        violations=violations,
        delivered=[] if delivered is None else delivered,
        moved=[] if moved is None else moved,
    )
    try:
        yield violations
    finally:
        _WATCH.clear()


def test_the_oracle_sees_every_change_outside_the_run_root(tmp_path):
    """The teeth of the sweep and the audit hook: each way of changing a path outside the run
    root is seen by both, a write inside the run root by neither, and a write under a named
    destination is admitted only from `xeda/deliver.py`."""
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


def test_the_oracle_sees_every_use_of_the_working_directory(tmp_path):
    """The teeth of the audit hook for the working directory: a change of it, a path resolved
    against it and a process started in it are each seen, while an absolute path, a path relative
    to a directory descriptor and a process with an explicit `cwd=` are not."""
    world = _world(tmp_path)
    world.root.mkdir()
    here = os.getcwd()
    quiet = [sys.executable, "-c", ""]
    with watching(world) as violations:
        os.chdir(here)  # changes nothing, and is seen
        with contextlib.chdir(here):  # two changes: in, and back
            pass
        os.listdir(".")
        try:
            open("a-file-that-is-not-there")
        except FileNotFoundError:
            pass
        shutil.rmtree("a-directory-that-is-not-there", ignore_errors=True)
        try:
            list(os.fwalk("a-directory-that-is-not-there"))
        except FileNotFoundError:
            pass
        descriptor = os.open(world.root, os.O_RDONLY)
        try:
            os.link("a-file-that-is-not-there", "linked", dst_dir_fd=descriptor)
        except FileNotFoundError:
            pass
        finally:
            os.close(descriptor)
        subprocess.run(quiet, cwd=None, check=True)
        subprocess.run(quiet, cwd=".", check=True)
        seen = list(violations)
        violations.clear()
        # what the hook admits
        (world.work / "sqrt.vhdl").read_bytes()
        os.listdir(world.work)
        subprocess.run(quiet, cwd=world.work, check=True)
        descriptor = os.open(world.root, os.O_RDONLY)
        try:
            os.mkdir("made", dir_fd=descriptor)
            os.rmdir("made", dir_fd=descriptor)
            os.listdir(descriptor)
        finally:
            os.close(descriptor)
    assert seen[:8] == [
        f"os.chdir {here}",
        f"os.chdir {here}",
        f"os.chdir {here}",
        "relative os.listdir .",
        "relative open a-file-that-is-not-there",
        "relative shutil.rmtree a-directory-that-is-not-there",
        "relative os.fwalk a-directory-that-is-not-there",
        "relative os.link a-file-that-is-not-there",
    ], seen
    assert len(seen) == 10, seen
    assert all(text.startswith("process without an absolute cwd") for text in seen[8:]), seen
    assert not violations, violations


# --- the launches ---------------------------------------------------------------------------


#: Options that make a flow write what it derives from its settings (a waveform's format, the
#: mode of synthesis, a library merged), which the flipped options do not all reach.
HAND_VARIANTS: Dict[str, List[Dict[str, Any]]] = {
    "ghdl_sim": [{"vcd": "w.vcd"}, {"fst": "w.fst"}, {"wave": "w.ghw"}],
    "nvc": [{"vcd": "w.vcd"}, {"fst": "w.fst"}],
    "vcs": [
        {"ucli_script": "notes.txt"},
        {"gui": True},
        {"sdf_file": "notes.txt", "sdf_instance": "uut"},
    ],
    "vivado_sim": [{"nthreads": 2}, {"sdf": {"max": "notes.txt", "root": "uut"}}],
    "vivado_synth": [{"bitstream": "outputs/x.bit"}, {"out_of_context": True}],
    "vivado_alt_synth": [{"out_of_context": True, "flatten_hierarchy": "full"}],
    "vivado_project": [{"out_of_context": True, "flatten_hierarchy": "full"}],
    "yosys_fpga": [{"abc_script": "notes.txt", "keep_hierarchy": ["sqrt"]}],
    "dc": [{"__language": "2019"}, {"__language": "1993"}],
    "ise_synth": [{"__language": "2019"}],
}
#: The flows whose launch with the plain testbench (`variant_design`) does not reach their
#: `run()`, and why nothing is lost. Any other that does not is a failure of the sweep: it would
#: see nothing of it.
NEVER_RUN: dict[str, str] = {}
#: Settings that start a container or reach a machine: never flipped.
NOT_FLIPPED = ("docker", "remote", "ssh", "junest", "image")
#: The flows whose variant launches (those of `launch_plan` beyond the flow's own settings) run a
#: plain VHDL testbench, so that the stood-in simulator is reached: `vivado_power` cannot run the
#: sweep's cocotb one (`UNPLANNABLE`).
PLAIN_TB_FLOWS = ("ghdl_sim", "nvc", "vivado_sim", "vcs", "vivado_power")


class Launch(NamedTuple):
    """One launch of a case: how it differs from the launch that its scenario describes."""

    label: str
    settings: Optional[dict] = None  # None: `sweep_settings`, with what the scenario adds
    launcher: dict = {}  # added to what the scenario gives the launcher
    variant: bool = False  # the design of `variant_design`, not the sweep's
    strict: bool = True  # an FPGA flow with a fake toolchain has to end "ok"


@dataclass
class Seen:
    """What the launches of a case left in the process, as `_launch` saw it: the problems, the
    flows that finished (their objects are compared with the state of the package at the end), and
    the processes that the launches asked for."""

    problems: List[str] = field(default_factory=list)
    finished: List[tuple] = field(default_factory=list)
    processes: "ProcessRecorder" = field(default_factory=lambda: ProcessRecorder())


def variant_design(flow_name: str) -> tuple:
    """The design of a variant launch: the sweep's, but a plain testbench for `PLAIN_TB_FLOWS`."""
    if flow_name not in PLAIN_TB_FLOWS:
        return DESIGNS.get(flow_name, SQRT_DESIGN)
    rtl, tb = PLAIN_TB_DESIGN
    if flow_name in ("vivado_sim", "vivado_power"):
        tb = {**tb, "uut": "uut"}  # the switching activity is recorded for the unit's instance
    return rtl, tb


def _flip_candidates(flow_class, current: dict):
    """(name, value) for each boolean, literal and enumerated option, set to a value other than
    its value in `current`, or its default when `current` does not set it."""
    for name, info in flow_class.Settings.model_fields.items():
        if any(part in name for part in NOT_FLIPPED):
            continue
        annotation, default = info.annotation, current.get(name, info.default)
        if annotation is bool or annotation == Optional[bool]:
            yield name, (not default) if isinstance(default, bool) else True
        elif typing.get_origin(annotation) is typing.Literal:
            options = [o for o in typing.get_args(annotation) if o != default and o is not None]
            if options:
                yield name, options[0]
        elif isinstance(annotation, type) and issubclass(annotation, enum.Enum):
            options = [o for o in annotation if o != default]
            if options:
                yield name, options[0].value


def flipped_settings(flow_class, base: dict, world: World) -> dict:
    """`base`, with every option flipped that still validates beside the others."""
    settings = dict(base)
    for name, value in _flip_candidates(flow_class, settings):
        trial = {**settings, name: value}
        try:
            flow_class.Settings.from_input(trial, design_root=world.work, runner_cwd=world.work)
        except Exception:  # noqa: BLE001 - a combination the flow refuses is left out
            continue
        settings = trial
    return settings


def launch_plan(flow_class, world: World, scenario: str) -> Optional[List[Launch]]:
    """The launches of a case, or None for the scenario's own (`twice` launches twice, any other
    once). The launches the process oracle needs ride on the cases that already launch the flow:
    `twice` adds a rebuild and the options that write what the settings derive; `clean` adds a
    launch with every option flipped, and with debug."""
    base = sweep_settings(flow_class, world)
    if scenario == "twice":
        return [
            Launch("its own settings"),
            Launch("its own settings again"),
            Launch(
                "its own settings, rebuilt",
                base,
                {"rebuild_all": True},
                variant=True,
                strict=False,
            ),
            *[
                Launch(
                    f"options {extra}",
                    {**base, **extra},
                    {},
                    variant=True,
                    strict=False,
                )
                for extra in HAND_VARIANTS.get(flow_class.name, [])
            ],
        ]
    if scenario == "clean":
        return [
            Launch("its own settings"),
            Launch(
                "every option flipped, with debug",
                flipped_settings(flow_class, base, world),
                {"debug": True},
                variant=True,
                strict=False,
            ),
        ]
    return None


def _launch(
    flow_class,
    world: World,
    monkeypatch,
    scenario: str,
    reached: list,
    plan: Optional[List[Launch]] = None,
    seen: Optional[Seen] = None,
) -> str:
    """Launch `flow_class` as `scenario` says, in the launches of
    `plan`; how each launch ended ("ok", "failed", or the exception's class name). `reached`
    gets the label of each launch whose flow reached its `run()`, and `seen` what the launches
    left in the process."""
    seen = Seen() if seen is None else seen
    use_fake_tools(monkeypatch)
    if flow_class.name in FPGA_FAKED:
        from .tool_utils import use_fake_fpga_tools

        ensure_run_root(world.root)
        use_fake_fpga_tools(monkeypatch, world.root / FAKE_TOOLCHAIN)
    seen.processes.install(monkeypatch)
    seen.processes.answers = stand_in_answers(world)
    if flow_class.name not in FAKED:
        monkeypatch.setattr("xeda.tool.run_process", seen.processes.stub)
        monkeypatch.setattr("xeda.tool.Tool.version_gte", lambda self, *args: True)
    run = flow_class.run
    launching = [""]
    monkeypatch.setattr(flow_class, "run", lambda self: reached.append(launching[0]) or run(self))
    own_settings = sweep_settings(flow_class, world)
    launcher: dict = {"display_results": False}
    if scenario == "clean":
        launcher["clean"] = True
    elif scenario == "purge":
        launcher.update(post_cleanup=True, post_cleanup_purge=True)
    elif scenario in DELIVERING:
        if scenario == "purged_delivered":
            launcher.update(post_cleanup=True, post_cleanup_purge=True)
        located = LOCATED.get(flow_class.name, {})
        # a file that two deliveries name is copied, never moved: a flow with a located
        # deliverable has `--outputs-to` deliver its other artifacts only
        if flow_class.action_reason is None and not (located and scenario == "purged_delivered"):
            # a programmer writes no outputs: `--outputs-to` is refused before it runs
            # (`test_fpga_chains.py`), so it delivers only what it names
            launcher["outputs_to"] = world.delivered
        for key, name in located.items():
            own_settings[key] = str(world.delivered / name)
    if plan is None:
        plan = [Launch("its own settings")] * (2 if scenario == "twice" else 1)
    outcomes = []
    errors = []
    strict = []
    for launch in plan:
        launching[0] = launch.label
        rtl, tb = (
            variant_design(flow_class.name)
            if launch.variant
            else DESIGNS.get(flow_class.name, SQRT_DESIGN)
        )
        settings = dict(own_settings if launch.settings is None else launch.settings)
        standard = settings.pop("__language", "2008")
        design = Design(
            name="sqrt",  # every flow's, so the canaries named after it (`sqrt.xpr`) apply to all
            root=world.work,
            rtl=rtl,
            tb=tb,
            language={"vhdl": {"standard": standard}},
        )
        asked_settings, asked_design = copy.deepcopy(settings), design.model_dump_json()
        process_before = ss.process_state()
        written = {"output": "", "error": ""}
        try:
            runner = DefaultRunner(world.root, **{**launcher, **launch.launcher})
            with ss.streams_written() as written:
                flow = runner.launch_flow(flow_class, design, settings)
            outcomes.append("ok" if flow.succeeded else "failed")
            seen.finished.append((launch.label, flow))
        except Exception as e:  # noqa: BLE001 - how it ends is compared, not judged
            outcomes.append(type(e).__name__)
            errors.append(str(e))
        if launch.strict:
            strict.append(outcomes[-1])
        # judged here as well as at the end of the case: the working directory and the
        # environment the stand-in tools set are put back when they are done, and would hide a
        # change made meanwhile
        changes = [
            f"process: {line}" for line in ss.process_changes(process_before, ss.process_state())
        ]
        changes += [
            f"the launch wrote to the standard {stream}: {text.strip()[:200]!r}"
            for stream, text in written.items()
            if text.strip()
        ]
        if settings != asked_settings:
            changes.append("the settings mapping the caller gave changed")
        if design.model_dump_json() != asked_design:
            changes.append("the Design the caller gave changed")
        seen.problems += [f"{launch.label}: {line}" for line in changes]
    if flow_class.name in FPGA_FAKED:
        assert all(
            outcome == "ok" for outcome in strict
        ), f"{flow_class.name} lost positive isolation coverage: {outcomes}: {errors}"
    return ",".join(outcomes)


def process_leftovers(state_before, process_before, finished: list) -> List[str]:
    """What the launches left in the process, once their stand-in tools are put back: a change of
    the tables, defaults and class attributes of the package, a change of the process, and a
    mutable object that a finished flow shares with the package."""
    problems = [f"state of the package: {line}" for line in ss.changes(state_before, ss.state())]
    problems += [
        f"process: {line}" for line in ss.process_changes(process_before, ss.process_state())
    ]
    shared = ss.shared_objects()
    for label, flow in finished:
        mine = ss.reachable(flow, "flow")
        problems += [
            f"{label}: {mine[oid][0]} ({mine[oid][1]}) is also {shared[oid][0]}"
            for oid in mine.keys() & shared.keys()
        ]
    return problems


FLOWS = [cls for cls, _ in flow_classes()]
#: the scenarios that deliver outputs (`purged_delivered` deletes the run directories after, so
#: delivery moves the files)
DELIVERING = ("delivered", "purged_delivered")
#: flows whose located deliverable the sweep's launch really delivers: under a purge, by move
MOVING = {"vivado_synth", "vivado_sim", "yosys_fpga", "nextpnr", "fpga_pack"}


def _flow(name: str):
    return next(cls for cls in FLOWS if cls.name == name)


def _named(world: World, flow_name: str, scenario: str, delivered: List[Path]) -> set:
    """What a launch may change outside its run root, relative to `world.parent`: each
    destination it named -- a located deliverable, and the `outputs_to` directory itself -- and,
    in that directory, each file delivery wrote there and the directories it made on the way.
    Nothing else in it: a tool's file beside a delivered one is a change."""
    if scenario not in DELIVERING:
        return set()
    named = {world.delivered, *(world.delivered / n for n in LOCATED.get(flow_name, {}).values())}
    for path in delivered:
        named.update(p for p in (path, *path.parents) if p.is_relative_to(world.delivered))
    return {str(p.relative_to(world.parent)) for p in named}


def _sweep(
    flow_class,
    world: World,
    monkeypatch,
    scenario: str,
    delivered: Optional[List[Path]] = None,
    moved: Optional[List[Path]] = None,
    plan: Optional[List[Launch]] = None,
    seen: Optional[Seen] = None,
) -> tuple:
    """One launch of the sweep: what changed outside the run root but the destinations it named
    what the audit hook saw, and whether the flow reached its `run()`. `delivered` collects the
    files delivery renamed into place, `moved` those it moved out of a run directory."""
    before = _state(world.parent, [world.root])
    reached: list = []
    delivered = [] if delivered is None else delivered
    with watching(world, delivered, moved) as violations:
        _launch(flow_class, world, monkeypatch, scenario, reached, plan, seen)
    after = _state(world.parent, [world.root])
    named = _named(world, flow_class.name, scenario, delivered)
    changed = sorted(
        k for k in before.keys() | after.keys() if before.get(k) != after.get(k) and k not in named
    )
    return changed, violations, reached


@pytest.mark.parametrize("scenario", ["twice", "clean", "purge", "delivered", "purged_delivered"])
@pytest.mark.parametrize("flow_class", FLOWS, ids=lambda c: c.name)
def test_nothing_outside_the_run_root_changes_but_what_was_named(flow_class, scenario, tmp_path):
    """No change outside the run root, seen in the file system or by the audit hook. Where the
    run directories are deleted after the delivery, every file delivered was moved, and no
    temporary name is left. The same launches leave nothing in the process: not in the tables,
    defaults and class attributes of the package, nor in the working directory, the environment,
    `sys.path`, the logging and the warning filters; they share no mutable object with the
    package; and they leave the settings mapping and the `Design` of the caller as they were."""
    ss.import_package()
    world = _world(tmp_path)
    plan = launch_plan(flow_class, world, scenario)
    delivered: List[Path] = []
    moved: List[Path] = []
    seen = Seen()
    state_before, process_before = ss.state(), ss.process_state()
    with pytest.MonkeyPatch.context() as mp:
        changed, violations, reached = _sweep(
            flow_class, world, mp, scenario, delivered, moved, plan, seen
        )
    assert not changed, f"{flow_class.name} changed {changed}"
    assert not violations, f"{flow_class.name}: {violations}"
    problems = [*seen.problems, *process_leftovers(state_before, process_before, seen.finished)]
    assert not problems, f"{flow_class.name} left in the process:\n" + "\n".join(problems)
    misplaced = process_findings(seen.processes.events)
    assert not misplaced, f"{flow_class.name} started processes in the wrong place:\n" + "\n".join(
        misplaced
    )
    if scenario == "twice":  # the sweep keeps its teeth
        if flow_class.name not in UNREACHED:
            ran = [
                e
                for e in seen.processes.events
                if e.flow is not None and e.flow.name == flow_class.name and not e.probe
            ]
            assert bool(ran) is (
                flow_class.name not in NO_PROCESS_IN_RUN
            ), f"{flow_class.name}: " + NO_PROCESS_IN_RUN.get(
                flow_class.name, "it asked for no process"
            )
        own = "its own settings" in reached
        assert own is (flow_class.name not in UNREACHED), UNREACHED.get(flow_class.name)
        # a launch that fails before `run()` is judged clean by everything above: say so
        assert flow_class.name in NEVER_RUN or "its own settings, rebuilt" in reached, (
            f"{flow_class.name}: the launch with the plain testbench did not reach run(), "
            "so nothing was seen of it"
        )
    if scenario == "purged_delivered":  # and the move is what it sweeps
        assert len(moved) == len(delivered), f"{flow_class.name}: {moved} moved, {delivered} made"
        assert bool(moved) or flow_class.name not in MOVING, f"{flow_class.name} moved nothing"
    else:
        assert not moved, f"{flow_class.name} moved files without a purge: {moved}"


def _also_runs(flow_class, monkeypatch, command: str) -> None:
    """`flow_class.run` runs `command` in a child process after the flow's own `run()`: a
    tool's write, which only the file system shows, never the audit hook."""
    run = flow_class.run

    def run_and_command(self):
        run(self)
        subprocess.run(["sh", "-c", command], check=True, cwd=self.run_path)

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
    assert not violations, "a child process's write: the audit hook cannot see it"


def test_the_sweep_sees_a_touch_only_change(tmp_path, monkeypatch):
    """A tool that only sets the modification time of a file of the user's fails the sweep."""
    world = _world(tmp_path)
    vivado_synth = _flow("vivado_synth")
    _also_runs(vivado_synth, monkeypatch, f"touch -m -t 200001010000 '{world.work / 'notes.txt'}'")
    changed, violations, _ = _sweep(vivado_synth, world, monkeypatch, "clean")
    assert changed == ["work/notes.txt"]
    assert not violations, "a child process's write: the audit hook cannot see it"


# --- what a tool is handed ------------------------------------------------------------------

#: The documents xeda writes into a run directory: they name every path of the launch, and no
#: tool reads them.
LAUNCH_DOCUMENTS = ("settings.json", "results.json", "trace.json", "trace.json.tmp")
#: A path under the run root, up to a character that ends a word in a script or a command line.
_PATH_END = r"[^\s\"'`;,()<>|&{}\[\]=:]*"


def declared_values(flow) -> list[Path]:
    """Every file or directory that `flow` declared as an input (`Flow.Inputs`), as a path.

    The `design` input is no file: it is the design that a converter derived. Its files are the
    ones the converter generated, which the record of the input lists."""
    values: list[Path] = []
    for name in type(flow.inputs).model_fields:
        value = getattr(flow.inputs, name)
        values += [Path(v) for v in (value if isinstance(value, (list, tuple)) else [value]) if v]
    for record in getattr(flow, "declared_input_records", ()):
        if record.name == "design":
            values += [Path(path) for path in record.paths]
    return values


class Started(NamedTuple):
    """A process a flow started: the directory it ran in, and its command line and environment."""

    cwd: Path
    text: str


def run_root_paths(text: str, root: Path, cwd: Path | None) -> set[Path]:
    """The paths in `text` that lie under `root`: written in full (as they are and as they
    resolve), or, if `cwd` is given, relative to it (`../producer/results.json`); and `cwd`
    itself, if it lies under the root."""
    found: set[Path] = set()
    for base in {str(root), str(root.resolve())}:
        found.update(Path(m) for m in re.findall(re.escape(base) + _PATH_END, text))
    if cwd is not None:
        # a tool that runs in the run root, or in another flow's directory, reads what it names
        # relative to there
        for place in (cwd, *cwd.parents):
            if place == root or place == root.resolve():
                found.add(cwd)
                break
        for word in re.findall(r"[^\s\"'`;,()<>|&{}\[\]=:]+", text):
            if not word.startswith("/") and "/" in word:
                path = Path(os.path.normpath(cwd / word))
                if path.is_relative_to(root) or path.is_relative_to(root.resolve()):
                    found.add(path)
    return found


def handed_paths(flow, root: Path, started: Sequence[Started]) -> set[Path]:
    """The paths under the run root that `flow`'s tools were handed: in the command lines and
    environments of the processes `started` holds, and in the text files its run directory holds
    (the scripts, the generated constraint files, the recorded calls of a stand-in tool, the
    tools' logs), but not the launch's own documents."""
    found: set[Path] = set()
    for process in started:
        found |= run_root_paths(process.text, root, process.cwd)
    for path in sorted(flow.run_path.rglob("*")):
        if path.is_symlink() or not path.is_file() or path.name in LAUNCH_DOCUMENTS:
            continue
        # a report is what a tool wrote, with the command lines it was run by: only a path written
        # in full counts there; elsewhere (a script, a generated constraint file) a step up does
        written = path.is_relative_to(flow.run_path / "reports")
        try:
            found |= run_root_paths(path.read_text(), root, None if written else flow.run_path)
        except (UnicodeDecodeError, OSError):
            continue
    return found


def foreign_paths(flow, root: Path, handed: set[Path]) -> list[str]:
    """What `handed` names that `flow` may not be handed: a path under the run root, outside the
    flow's own run directory, that is not a declared input of the flow or a file it registered as
    an implicit input (the chip database, a bundled board's file). One rule for a producer's
    files: they reach a consumer as its declared inputs, whatever else lies in the directory."""

    def inside(path: Path, base: Path) -> bool:
        return path.is_relative_to(base.resolve())

    #: the stand-in tools are installed under the run root, and a tool is found by the path to it
    allowed = [flow.run_path, *declared_values(flow), *flow.implicit_inputs, root / FAKE_TOOLCHAIN]
    # A path counts by where it leads: a link in the flow's own directory to a producer's file is
    # the producer's file, and a link to a declared input is that input.
    found = {path: path.resolve() for path in handed}
    return sorted(
        str(path)
        for path, resolved in found.items()
        if resolved.is_relative_to(root.resolve())
        and not any(inside(resolved, base) for base in allowed)
    )


def _flow_in_the_stack() -> Flow | None:
    """The flow whose code is running: the innermost `self` on the stack that is a flow. A
    producer started from a consumer's launch is the innermost one, and a tool that a helper
    (the chip database's generator) starts is the flow's that called the helper."""
    frame = sys._getframe(1)
    while frame is not None:
        candidate = frame.f_locals.get("self")
        if isinstance(candidate, Flow):
            return candidate
        frame = frame.f_back  # type: ignore[assignment]
    return None


#: The code that makes the scratch directory of a probe: a version query of a tool, and the CPU
#: query of a container (`xeda/tool.py`, by qualified name). A directory that any other code
#: makes is no probe's, whatever its name.
PROBE_ALLOCATORS = ("Tool.probe_stdout", "Docker.cpuinfo")
_TOOL_MODULE = Path(xeda_tool.__file__).resolve()
_POPEN_SIGNATURE = inspect.signature(subprocess.Popen.__init__)
_RUN_PROCESS_SIGNATURE = inspect.signature(run_process)


class ProcessEvent(NamedTuple):
    """A request to start a process, as it was when made."""

    flow: Flow | None  # the innermost flow on the stack; None: no flow was running it
    operation: str  # the program and its first argument: `docker image`
    raw_cwd: object  # the `cwd` as given: None, a relative path or an absolute one
    cwd: Path  # the directory it starts in, resolved then (for no `cwd`, the current one)
    text: str  # the command line, and the environment the flow added
    stubbed: bool  # a request to the stand-in of `run_process`, not a real `Popen`
    probe: bool  # `cwd` was the active scratch directory of a probe


@dataclass
class ProcessRecorder:
    """Every process the code under test asks for, kept in order: a real `Popen`, and a request
    to the stand-in of `run_process` (`stub`), which flows without a fake tool get. The owner of
    an event is the innermost flow on the stack when it was made. The scratch directories that
    `PROBE_ALLOCATORS` make are known while they exist."""

    events: List[ProcessEvent] = field(default_factory=list)
    probes: set = field(default_factory=set)
    #: what `stub` prints for a query (`stand_in_answers`); any other request prints nothing
    answers: Dict[Tuple[str, str], str] = field(default_factory=dict)

    def note(self, words: List[str], raw_cwd: object, env: Any, stubbed: bool) -> None:
        # `PWD` is the directory of the process (`run_process` sets it), which a finding names.
        added = {k: v for k, v in (env or {}).items() if k != "PWD" and os.environ.get(k) != v}
        text = " ".join([*words, *(f"{k}={v}" for k, v in added.items())])
        cwd = Path(os.path.realpath(Path.cwd() if raw_cwd is None else os.fsdecode(raw_cwd)))
        self.events.append(
            ProcessEvent(
                _flow_in_the_stack(),
                " ".join([os.path.basename(words[0]), *words[1:2]]) if words else "",
                raw_cwd,
                cwd,
                text,
                stubbed,
                raw_cwd is not None and os.path.isabs(os.fsdecode(raw_cwd)) and cwd in self.probes,
            )
        )

    def stub(self, *args: Any, **kwargs: Any) -> str:
        """Stand in for `run_process`: record the request, start nothing."""
        given = _RUN_PROCESS_SIGNATURE.bind(*args, **kwargs).arguments
        words = [str(given["executable"]), *map(str, given.get("args") or [])]
        self.note(words, given.get("cwd"), given.get("env"), stubbed=True)
        return self.answers.get(
            (os.path.basename(words[0]), words[1] if len(words) > 1 else ""), ""
        )

    def install(self, monkeypatch) -> None:
        """Record from now on, until `monkeypatch` is undone: at `subprocess.Popen`, where every
        process starts whichever way the code asked (`Tool.execute`, `run_process`, a helper's own
        call), and at the allocation of scratch directories."""
        popen_init, recorder = subprocess.Popen.__init__, self

        def recording(popen, *args, **kwargs):
            try:
                given = _POPEN_SIGNATURE.bind(popen, *args, **kwargs).arguments
            except TypeError:  # a call `Popen` refuses itself
                return popen_init(popen, *args, **kwargs)
            command = given["args"]
            words = [command] if isinstance(command, (str, os.PathLike)) else list(command)
            recorder.note(list(map(str, words)), given.get("cwd"), given.get("env"), False)
            return popen_init(popen, *args, **kwargs)

        class Probing(tempfile.TemporaryDirectory):  # type: ignore[type-arg]
            """A scratch directory that is known while it exists if a probe made it."""

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                super().__init__(*args, **kwargs)
                code = sys._getframe(1).f_code
                self.known = None
                if (
                    code.co_qualname in PROBE_ALLOCATORS
                    and Path(code.co_filename).resolve() == _TOOL_MODULE
                ):
                    self.known = Path(self.name).resolve()
                    recorder.probes.add(self.known)

            def cleanup(self) -> None:
                recorder.probes.discard(self.known)
                super().cleanup()

        monkeypatch.setattr(subprocess.Popen, "__init__", recording)
        monkeypatch.setattr(tempfile, "TemporaryDirectory", Probing)


#: The processes that no flow runs and that may start anywhere, by operation (the program and its
#: first argument, as `ProcessEvent.operation`), each with the reason. None does.
REVIEWED_UNOWNED_PROCESSES: Dict[str, str] = {}


def process_findings(events: Sequence[ProcessEvent]) -> list[str]:
    """What the processes of `events` did wrong. A process of a flow starts with an absolute
    `cwd`, in the claimed run directory of that flow (`flow.run_directory.path`, resolved: a link
    that leads out of it does not count) or in the active scratch directory of a probe; a
    process of no flow is a finding unless its operation is reviewed. A tool that starts in the
    directory of the caller writes its logs and journals there."""
    findings = []
    for event in events:
        verb = "requested" if event.stubbed else "started"
        what = f"{event.flow.name if event.flow else 'no flow'} {verb} `{event.text[:120]}`"
        if event.flow is None:
            if event.operation not in REVIEWED_UNOWNED_PROCESSES:
                findings.append(f"{what} in {event.cwd}: it is no flow's process")
            continue
        run = getattr(event.flow, "run_directory", None)
        if event.raw_cwd is None:
            findings.append(f"{what} with no cwd (it starts in {event.cwd})")
        elif not os.path.isabs(os.fsdecode(event.raw_cwd)):
            findings.append(f"{what} with the relative cwd {event.raw_cwd!s} ({event.cwd})")
        elif event.probe:
            continue
        elif run is None:
            findings.append(f"{what} in {event.cwd}: its flow has no run directory")
        elif not event.cwd.is_relative_to(run.path):
            findings.append(f"{what} in {event.cwd}, outside its run directory {run.path}")
    return findings


def _launched(
    flow_class,
    world: World | None,
    monkeypatch,
    launch_it=None,
    plan: Optional[List[Launch]] = None,
) -> tuple[list[Flow], ProcessRecorder]:
    """Launch `flow_class` as the sweep does (in the launches of `plan`), or run `launch_it`: the
    flows the request's runner launched, and every process that was asked for, those of flows that
    did not finish included."""
    runners: list[DefaultRunner] = []
    launch = DefaultRunner.launch_flow

    def spying(self, *args, **kwargs):
        runners.append(self)
        return launch(self, *args, **kwargs)

    monkeypatch.setattr(DefaultRunner, "launch_flow", spying)
    seen = Seen()
    if launch_it is None:
        _launch(flow_class, world, monkeypatch, "clean", [], plan, seen)
    else:
        seen.processes.install(monkeypatch)
        launch_it()
    return list(runners[0].launched), seen.processes  # the request's; a producer has its own


def _tools_handed(
    flow_class, world: World | None, monkeypatch, launch_it=None
) -> list[tuple[Flow, Sequence[Started]]]:
    """Launch `flow_class` as the sweep does; its launched flows, each with the processes it
    started (the real ones)."""
    flows, processes = _launched(flow_class, world, monkeypatch, launch_it)
    return [
        (
            flow,
            [Started(e.cwd, e.text) for e in processes.events if e.flow is flow and not e.stubbed],
        )
        for flow in flows
    ]


@pytest.mark.parametrize("flow_class", FLOWS, ids=lambda c: c.name)
def test_a_tool_is_handed_no_file_of_another_flow_but_its_declared_inputs(
    flow_class, tmp_path, monkeypatch
):
    """The runtime half of "no flow reads another flow's state": under the stand-in tools, every
    path under the run root that a flow's tool is handed lies in the flow's own run directory, or
    is a declared input, or an input the flow registered (`foreign_paths`). A consumer is judged
    on what it declares, so a file it read by another way would not be noticed when it changed."""
    world = _world(tmp_path)
    for flow, started in _tools_handed(flow_class, world, monkeypatch):
        handed = handed_paths(flow, world.root, started)
        assert (
            foreign_paths(flow, world.root, handed) == []
        ), f"{flow.name} was handed a foreign path"


def _starts_a_process_wrongly(how: str, world: World, monkeypatch) -> None:
    """Patch `nextpnr` so that, besides its own work, it starts `true --wrong` the way `how`
    says (`WRONG_STARTS`), or, for the starts outside its `run()`, in the place `how` names."""
    from xeda.flow_runner import launch_run
    from xeda.flows import Nextpnr

    run = Nextpnr.run

    def run_and_start(self):
        run(self)
        here = self.run_path
        if how == "caller's directory":
            run_process("true", ["--wrong"], cwd=world.work)
        elif how == "no cwd":
            run_process("true", ["--wrong"])
        elif how == "relative cwd":  # it leads into the run directory, from where we are
            run_process("true", ["--wrong"], cwd=Path(os.path.relpath(here, Path.cwd())))
        elif how == "producer's directory":
            run_process("true", ["--wrong"], cwd=self.inputs.netlist.parent)
        elif how == "link that leads out":
            (here / "escape").symlink_to(world.outside, target_is_directory=True)
            run_process("true", ["--wrong"], cwd=here / "escape")
        else:
            assert how == "forged probe directory"
            with tempfile.TemporaryDirectory(prefix="xeda-probe-", dir=world.outside) as forged:
                run_process("true", ["--wrong"], cwd=Path(forged))

    if how == "no flow":
        free_working_locations = launch_run.free_working_locations

        started: list = []

        def free_and_start(flow):  # launcher code: `flow` is no `self`
            if not started:
                started.append(flow)
                run_process("true", ["--wrong"], cwd=flow.run_path)
            free_working_locations(flow)

        monkeypatch.setattr(launch_run, "free_working_locations", free_and_start)
    elif how == "constructor that raises":
        init = Nextpnr.__init__

        def init_start_and_raise(self, *args, **kwargs):
            init(self, *args, **kwargs)
            run_process("true", ["--wrong"], cwd=world.work)
            raise RuntimeError("the constructor failed")

        monkeypatch.setattr(Nextpnr, "__init__", init_start_and_raise)
    else:
        monkeypatch.setattr(Nextpnr, "run", run_and_start)


#: How a flow starts a process wrongly, with what the finding says: the flow, the command, the
#: directory, and which part failed. Each way fails one branch of `process_findings` alone.
WRONG_STARTS = {
    "caller's directory": ("nextpnr started", "outside its run directory", "work"),
    "no cwd": ("nextpnr started", "with no cwd", "work"),
    "relative cwd": ("nextpnr started", "with the relative cwd", "nextpnr"),
    "producer's directory": ("nextpnr started", "outside its run directory", "yosys_fpga"),
    "link that leads out": ("nextpnr started", "outside its run directory", "outside"),
    "forged probe directory": ("nextpnr started", "outside its run directory", "xeda-probe-"),
    "no flow": ("no flow started", "it is no flow's process", "xeda_run"),
    "constructor that raises": ("nextpnr started", "outside its run directory", "work"),
}


@pytest.mark.parametrize("how", WRONG_STARTS)
def test_the_process_check_finds_a_process_that_does_not_start_in_its_run_directory(
    how, tmp_path, monkeypatch
):
    """Not vacuous: a process that starts in the directory of the caller, with no `cwd` or a
    relative one, in a producer's directory, through a link that leads out of the run directory,
    in a directory named like a probe's that no probe made, with no flow running it, or in a
    flow's constructor that then raises, is a finding that names the flow (or none), the command
    and the directory, and the part that failed. The one wrong start is the only finding."""
    from xeda.flows import Nextpnr

    world = _world(tmp_path)
    _starts_a_process_wrongly(how, world, monkeypatch)
    plan = [Launch("its own settings", strict=False)]  # the constructor that raises ends it
    flows, processes = _launched(Nextpnr, world, monkeypatch, plan=plan)
    (finding,) = process_findings(processes.events)
    who, failed, directory = WRONG_STARTS[how]
    assert finding.startswith(f"{who} `true --wrong`"), finding
    assert failed in finding, finding
    assert directory in finding, finding
    if how == "constructor that raises":  # it never reached the launcher's list
        assert "nextpnr" not in [flow.name for flow in flows]


def test_a_version_probe_in_a_flow_method_is_no_finding(tmp_path, monkeypatch):
    """A probe (`Tool.probe_stdout`) runs in its scratch directory, which no run directory
    holds: it is allowed, and the event shows it was a probe."""
    from xeda.flows import Nextpnr
    from xeda.tool import Tool

    run = Nextpnr.run

    def run_and_probe(self):
        run(self)
        Tool(executable="true", version_flag=None).probe_stdout("--probe")

    monkeypatch.setattr(Nextpnr, "run", run_and_probe)
    _, processes = _launched(Nextpnr, _world(tmp_path), monkeypatch)
    (probe,) = [e for e in processes.events if "--probe" in e.text]
    assert probe.flow is not None and probe.flow.name == "nextpnr"
    assert probe.probe and probe.cwd.name.startswith("xeda-probe-")
    assert not probe.cwd.is_relative_to(probe.flow.run_directory.path)
    assert process_findings(processes.events) == []
    assert probe.cwd not in processes.probes, "the scratch directory is no longer known when gone"


@pytest.mark.parametrize("flow_name", ["nextpnr", "fpga_pack", "openfpgaloader"])
def test_the_runtime_check_sees_a_consumer_handed_its_producer_s_declared_files(
    flow_name, tmp_path, monkeypatch
):
    """Not vacuous: a consumer's tool is handed paths of its producer's run directory, and every
    one of them is a declared input."""
    world = _world(tmp_path)
    flows = _tools_handed(_flow(flow_name), world, monkeypatch)
    consumer, started = next((f, c) for f, c in flows if f.name == flow_name)
    handed = handed_paths(consumer, world.root, started)
    producers = [f.run_path.resolve() for f, _ in flows if f is not consumer]
    assert producers, f"{flow_name} has no producer in the sweep's world"
    taken = {p for p in handed if any(r in (p, *p.resolve().parents) for r in producers)}
    assert taken, f"{flow_name}'s tool was handed no file of its producers"
    assert foreign_paths(consumer, world.root, handed) == []


def _reads_the_producer_s_results(how: str):
    """A `Nextpnr.run` that, besides its own work, hands the producer's `results.json` (not a
    declared input) to a process or a script, the way `how` says."""
    from xeda.flows import Nextpnr
    from xeda.proc_utils import run_process
    from xeda.tool import Tool

    run = Nextpnr.run

    def run_and_read_the_producer(self):
        run(self)
        undeclared = self.inputs.netlist.parent / "results.json"
        relative = os.path.relpath(undeclared, self.run_path)
        if how == "tool":
            Tool(executable="true").run("--read", undeclared)
        elif how == "tool, relative":
            Tool(executable="true").run(f"--read={relative}")
        elif how == "run_process":  # not through a Tool: as the chip database generator starts
            run_process("true", ["--read", str(undeclared)])
        elif how == "run_process, relative":
            run_process("true", [relative], cwd=self.run_path)
        elif how == "run root":  # a tool started in the run root, given a path relative to it
            root = self.run_path.parent.parent
            run_process("true", [os.path.relpath(undeclared, root)], cwd=root)
        elif how == "script":
            (self.run_path / "extra.tcl").write_text(f"source {undeclared}\n")
        else:
            assert how == "script, relative"
            (self.run_path / "extra.tcl").write_text(f'source "{relative}"\n')

    return run_and_read_the_producer


@pytest.mark.parametrize(
    "how",
    [
        "tool",
        "tool, relative",
        "run_process",
        "run_process, relative",
        "script",
        "script, relative",
    ],
)
def test_the_runtime_check_finds_a_consumer_handed_a_file_that_it_did_not_declare(
    how, tmp_path, monkeypatch
):
    """The producer's `results.json` is not a declared input of `nextpnr`: a tool that is handed
    it, in an argument or a script, written in full or relative to the run directory, by a `Tool`
    or by any other way to start a process, is a finding."""
    from xeda.flows import Nextpnr

    monkeypatch.setattr(Nextpnr, "run", _reads_the_producer_s_results(how))
    world = _world(tmp_path)
    flows = _tools_handed(Nextpnr, world, monkeypatch)
    consumer, started = next((f, c) for f, c in flows if f.name == "nextpnr")
    (producer,) = [f for f, _ in flows if f.name == "yosys_fpga"]
    undeclared = (producer.run_path / "results.json").resolve()
    assert undeclared not in {p.resolve() for p in declared_values(consumer)}
    handed = handed_paths(consumer, world.root, started)
    wanted = [str(undeclared)]
    if how == "run root":  # the run root itself is the tool's directory, which is no flow's
        wanted = sorted([str(world.root.resolve()), *wanted])
    assert foreign_paths(consumer, world.root, handed) == wanted


def _yosys_on_bluespec(tmp_path: Path, monkeypatch, patch=None):
    """Launch `yosys` on a copy of the Bluespec example, with real bsc and yosys: the flows that
    ran, each with the processes it started, and the run root."""
    require_bsc()
    require_yosys()
    work = tmp_path / "work"
    shutil.copytree(GCD, work)
    document = yaml.safe_load((work / "gcd.yaml").read_text())
    document["flows"] = {}  # no platform: yosys synthesizes for no library
    del document["targets"]
    (work / "gcd.yaml").write_text(yaml.safe_dump(document, sort_keys=False))
    design = Design.load(work / "gcd.yaml")
    root = tmp_path / "run"
    if patch is not None:
        patch(monkeypatch)

    def launch_it() -> None:
        flow = DefaultRunner(root, display_results=False).run_flow(
            "yosys", design, {"clocks": {"main_clock": {"period": "2 ns"}}}
        )
        assert flow is not None and flow.succeeded

    return _tools_handed(None, None, monkeypatch, launch_it), root


@pytest.mark.real_tool
def test_a_tool_is_handed_the_files_of_the_design_edge_and_no_other_file_of_its_converter(
    tmp_path, monkeypatch
):
    """The design input is a declared input: yosys reads the Verilog that bsc generated, which
    the record of the input lists. Without the record those files would be foreign."""
    flows, root = _yosys_on_bluespec(tmp_path, monkeypatch)
    consumer, started = next((f, c) for f, c in flows if f.name == "yosys")
    (converter,) = [f for f, _ in flows if f.name == "bsc"]
    handed = handed_paths(consumer, root, started)
    taken = {p for p in handed if converter.run_path.resolve() in (p, *p.resolve().parents)}
    assert taken, "yosys was handed no file of bsc"
    assert {p.name for p in taken} >= {"mkGcdUnit.v"}
    assert foreign_paths(consumer, root, handed) == []
    (record,) = [r for r in consumer.declared_input_records if r.name == "design"]
    assert {Path(path).resolve() for path in record.paths} >= {p.resolve() for p in taken}
    consumer.declared_input_records = ()
    assert sorted(foreign_paths(consumer, root, handed)) == sorted(map(str, taken))


def _reads_the_converter_s_results(monkeypatch) -> None:
    from xeda.flows import Yosys
    from xeda.tool import Tool

    run = Yosys.run

    def run_and_read_the_converter(self):
        run(self)
        Tool(executable="true").run("--read", self.run_path.parent / "bsc" / "results.json")

    monkeypatch.setattr(Yosys, "run", run_and_read_the_converter)


@pytest.mark.real_tool
def test_the_runtime_check_finds_a_consumer_handed_an_undeclared_file_of_its_converter(
    tmp_path, monkeypatch
):
    flows, root = _yosys_on_bluespec(tmp_path, monkeypatch, _reads_the_converter_s_results)
    consumer, started = next((f, c) for f, c in flows if f.name == "yosys")
    (converter,) = [f for f, _ in flows if f.name == "bsc"]
    undeclared = (converter.run_path / "results.json").resolve()
    assert foreign_paths(consumer, root, handed_paths(consumer, root, started)) == [str(undeclared)]


def test_the_runtime_check_follows_a_link_to_where_it_leads(tmp_path):
    """A link in the flow's own run directory to a file of a producer is that file; a link to a
    declared input is that input, and the run root itself is not a way into the producers."""
    from types import SimpleNamespace

    root = tmp_path / "run"
    consumer, producer = root / "d" / "consumer", root / "d" / "producer"
    for directory in (consumer, producer):
        directory.mkdir(parents=True)
    (producer / "out.txt").write_text("declared\n")
    (producer / "results.json").write_text("{}\n")
    (consumer / "declared").symlink_to(producer / "out.txt")
    (consumer / "undeclared").symlink_to(producer / "results.json")
    inputs = type("Inputs", (), {"model_fields": {"out": None}, "out": producer / "out.txt"})()
    flow = SimpleNamespace(run_path=consumer, inputs=inputs, implicit_inputs=[])
    handed = {consumer / "declared", consumer / "undeclared", root}
    assert foreign_paths(flow, root, handed) == [str(root), str(consumer / "undeclared")]


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
    """With the user's tree read-only (but the run root), a launch ends as it does with a
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
@pytest.mark.real_tool
def test_bsc_sim_simulates_the_bluespec_example_on_a_read_only_tree(tmp_path, monkeypatch):
    """With the real tools: bsc compiles the `gcd` example and Bluesim
    runs its Bluespec testbench, everything but the run root read-only."""
    require_bluesim()
    world = _world(tmp_path)
    ensure_run_root(world.root)
    rtl, tb = GCD_DESIGN
    design = Design(name="gcd", root=world.work, rtl=rtl, tb=tb)
    monkeypatch.chdir(world.work)
    _freeze(world.parent, keep=world.root)
    try:
        flow = DefaultRunner(world.root, display_results=False).launch_flow(
            "bsc_sim", design, {"vcd": "gcd.vcd"}
        )
    finally:
        _thaw(world.parent)
    assert flow.succeeded and (flow.run_path / "gcd.vcd").is_file()


#: A design generator: it writes the sources the design declares into its storage (`XEDA_GENERATED`,
#: under the run root), and counts its runs in the one place the oracle ignores, the run root.
GENERATOR = """\
import os, sys
from pathlib import Path

out = Path(os.environ["XEDA_GENERATED"])
(out / "gen").mkdir(exist_ok=True)
(out / "gen" / "top.v").write_text("// generated\\n")
with open(sys.argv[1], "a") as counter:
    counter.write("ran\\n")
"""


def test_a_launch_that_runs_a_generator_writes_only_under_the_run_root(tmp_path):
    """Loading a design runs nothing and writes nothing. The generation of a launch writes its
    storage and the lock beside it under the run root, and nothing in the design's tree: a
    second generation of the unchanged design generates nothing at all."""
    from xeda.flow_runner.launch_generation import generate

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
        f"    args: ['$DESIGN_ROOT/gen.py', {str(counter)!r}]\n"
        "    sources: ['spec.txt', 'gen.py']\n"
    )
    before = _state(world.parent, [world.root])
    with watching(world) as violations:
        design = Design.load(design_file)
        generate(design, run_root=world.root, environ=dict(os.environ))
        generate(design, run_root=world.root, environ=dict(os.environ))
    assert [src.path for src in design.rtl.sources] == [Path("gen/top.v")]
    assert counter.read_text() == "ran\n", "the second generation generated again"
    assert violations == []
    after = _state(world.parent, [world.root])
    assert before == after, "something outside the run root changed"
    (storage,) = (world.root / ".generated" / "generated").glob("*/")
    assert (storage / "gen" / "top.v").read_text() == "// generated\n"
    assert sorted(p.name for p in storage.parent.iterdir()) == [
        storage.name,
        storage.name + ".lock",
    ]


@pytest.mark.parametrize("flow, require", [("ghdl_sim", require_ghdl), ("nvc", require_nvc)])
@pytest.mark.real_tool
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
    args = [
        "run",
        "vivado_synth",
        "sqrt.yaml",
        "-s",
        "vivado_synth.fpga.part=xc7a12tcsg325-1",
        "--json",
    ]
    for extra in (["--clean"], ["--post-cleanup"], []):
        with watching(world) as violations:
            result = CliRunner().invoke(cli, [*args, *extra])
        assert result.exit_code == 0, result.output  # the oracle keeps its teeth
        assert not violations, violations
    assert _state(world.parent, [world.root]) == before


def test_a_targeted_run_and_its_scrub_change_nothing_outside_the_run_root(tmp_path, monkeypatch):
    """The target's directory is one level deeper in the run root and no further out: launching
    each of two targets (`--clean`, `--post-cleanup`, plain) and scrubbing them one by one and
    then all leaves everything outside the run root as it was, the audit hook seeing no write."""
    from xeda.console import console

    world = _world(tmp_path)
    text = (SQRT / "sqrt.yaml").read_text().rstrip("\n")
    (world.work / "sqrt.yaml").write_text(text + "\ntargets:\n  a: {}\n  b: {}\n")
    before = _state(world.parent, [world.root])
    monkeypatch.setenv("PATH", str(FAKE_TOOLS_DIR) + os.pathsep + os.environ["PATH"])
    monkeypatch.chdir(world.work)
    monkeypatch.setattr(console, "input", lambda *a, **kw: "yes")
    args = [
        "run",
        "vivado_synth",
        "sqrt.yaml",
        "-s",
        "vivado_synth.fpga.part=xc7a12tcsg325-1",
        "--json",
    ]
    for target in ("a", "b"):
        for extra in (["--clean"], ["--post-cleanup"], []):
            with watching(world) as violations:
                result = CliRunner().invoke(cli, [*args, "--target", target, *extra])
            assert result.exit_code == 0, result.output  # the oracle keeps its teeth
            assert not violations, violations
        assert (world.root / "sqrt" / target / "vivado_synth").is_dir()
    for extra in (["--target", "a"], []):
        with watching(world) as violations:
            result = CliRunner().invoke(cli, ["scrub", "vivado_synth", "sqrt", "--json", *extra])
        assert result.exit_code == 0, result.output
        assert not violations, violations
        assert not (world.root / "sqrt" / "a" / "vivado_synth").exists()
        assert extra or not (world.root / "sqrt" / "b" / "vivado_synth").exists()
    assert _state(world.parent, [world.root]) == before


INV_TOML = """name = "inv"
[rtl]
sources = ["inv.v"]
top = "inv"
clocks = ["clk"]
"""
INV_V = "module inv(input clk, input a, output reg y); always @(posedge clk) y <= ~a; endmodule\n"


@pytest.mark.parametrize("tools", ["fake xtclsh", "no xtclsh"])
def test_ise_synth_from_the_design_directory_deletes_none_of_its_files(
    tmp_path, monkeypatch, tools
):
    """The ise_synth probe: `xeda run ise_synth inv.toml --cwd --clean`, started in the
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
            "ise_synth.fpga.part=xc6slx9-2-tqg144",
            "ise_synth.clocks.main_clock.period=10ns",
        ],
    )
    assert _state(work, [work / "xeda_run"]) == before, "the design directory changed"


# --- the static scan ------------------------------------------------------------------------


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
    ("flow_runner/dependencies.py", "owner.remove(partial)"): (
        1,
        "RunDirectory.remove of the temporary directory of a failed fetch, which only this fetch "
        "names, under the exclusive lock of the storage",
    ),
    ("flow_runner/dependencies.py", 'owner.remove(into / ".git")'): (
        1,
        "RunDirectory.remove of the Git directory in the temporary directory of a fetch, which "
        "holds the URL and is no part of the content",
    ),
    ("flow_runner/dependencies.py", "os.rename(partial, storage)"): (
        1,
        "publish a complete temporary directory under the storage name, under the exclusive lock "
        "of the storage, through the link-refusing path guard",
    ),
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
        "`replacing_file`: its own complete temporary over the file it replaces; `live_log`: its "
        "own new temporary over the log's name at once. The name is one the caller located "
        "(`RunDirectory.writable`), and a rename replaces what is there as a name, never through it",
    ),
    (
        "utils.py",
        "Path(temporary).unlink(missing_ok=True)  # the temporary file, never committed",
    ): (
        1,
        "`_discard`: the temporary file `replacing_file` or `live_log` made with `mkstemp` and "
        "did not put at the name",
    ),
    ("flows/yosys/common.py", "flags.remove(flag)"): (1, "a list of flags"),
    ("proc_utils.py", "readable.remove(fd)"): (1, "a list of file descriptors"),
    ("deliver.py", "os.replace(temporary, destination)"): (
        1,
        "the delivery's own temporary file, renamed over the destination the rules allowed",
    ),
    ("deliver.py", "os.replace(source, temporary)"): (
        1,
        "a delivery by move, when the launch deletes the run directory right after: the run's "
        "own regular file, with no other name and inside the run directory by its resolved path "
        "(`Deliveries._move_into`), renamed over the delivery's own temporary placeholder "
        "beside the destination. It removes the run's name, never a file of the user's",
    ),
    ("deliver.py", "os.replace(temporary, source)"): (
        1,
        "the undo of that move: the run's file goes back to the name it was moved from. It "
        "replaces nothing: the name is free, as the move freed it under the run's lock",
    ),
    (
        "deliver.py",
        "temporary.unlink(missing_ok=True)  # its own temporary file, never anything else",
    ): (
        1,
        "its own temporary copy, after a failed copy or a failed re-check (`_discard`); a moved "
        "file is the run's output and is put back instead",
    ),
    ("deliver.py", "os.replace(temporary, self.record_path)"): (
        1,
        "the delivery record's temporary file, beside the run directory",
    ),
    ("platforms/providers.py", "source.rename(plan.directory)"): (
        1,
        "the install command writes the PDK store, outside a run: the platform directory of its "
        "own partial checkout, complete and checked, moved to its name in the store under the "
        "store's install lock; the name is free, as the install checked under the lock",
    ),
    ("platforms/providers.py", "path.unlink(missing_ok=True)"): (
        1,
        "the install command writes the PDK store, outside a run: the receipt that this install "
        "wrote where there was none, removed when the move of the platform directory failed",
    ),
    ("platforms/providers.py", "directory.rmdir()"): (
        1,
        "the install command writes the PDK store, outside a run: an empty parent directory that "
        "this install made, removed when the move failed (`rmdir` removes no file)",
    ),
    ("platforms/providers.py", "shutil.rmtree(path)"): (
        1,
        "the install command writes the PDK store, outside a run: the partial directory that "
        "this install made, removed after a failure or an interrupt (`rmtree` follows no link)",
    ),
    ("remote/manifest.py", "os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)"): (
        1,
        "the remote sync, outside a run directory (the inputs area of a workspace, or the "
        "mirror's staging area): a file that passed its SHA-256 check, renamed from its "
        "temporary name over the name in the same directory, which `parent` holds as an open "
        "descriptor reached without a link; a link at the name is replaced, never followed",
    ),
    ("remote/manifest.py", "os.unlink(name, dir_fd=parent)"): (
        1,
        "the remote sync, outside a run directory: a file, or a link as itself, removed by name "
        "in a directory that `parent` holds as an open descriptor reached without a link; "
        "`_remove_entry` removes a stale input, a leftover temporary file, or what is at a "
        "place that the manifest gives to another kind of entry",
    ),
    ("remote/agent.py", "shutil.rmtree(path)"): (
        1,
        "`xeda.remote.agent clean`, on the remote host outside any run directory: an environment, "
        "a code copy or a workspace of the remote root that nothing uses (no lock or running "
        "launch holds it). `clean` enters no link, and `rmtree` "
        "follows none",
    ),
    ("remote/agent.py", "os.rename(path, hidden)"): (
        1,
        "`xeda.remote.agent clean`, on the remote host outside any run directory: a workspace, "
        "an environment or a code copy whose lock `clean` holds is renamed to a hidden name in "
        "the same directory, so that no session works in it while `rmtree` deletes it. A rename replaces no file: the hidden "
        "name has the process id of `clean`",
    ),
    ("remote/manifest.py", "os.rmdir(name, dir_fd=parent)"): (
        1,
        "the remote sync, outside a run directory: a directory that `_remove_entry` has emptied "
        "(it enters a directory only by `O_NOFOLLOW` and removes a link as a link); `rmdir` "
        "removes no file",
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


#: In a tool script (every file of xeda's that is not Python: templates, the platforms' scripts):
#: a deletion (`file delete`, `file rename`, `rm`), or a tool command told to replace or overwrite
#: what is there (`-force`, `-overwrite`).
SCRIPT_DELETION = re.compile(r"\brm\b|\bfile\s+(delete|rename)\b|(?<![\w-])-(force|overwrite)\b")
#: a tool command a flow runs that deletes where it runs (`ghdl remove`)
TOOL_DELETION = re.compile(r"\.run\(\s*[\"'](--)?(remove|clean)[\"']")

_REPORT = "a report, output or checkpoint the tool writes under its own name in the flow's "
_REPORT += "reports/outputs/checkpoints directory"
_BITSTREAM = "the bitstream named by the flow's `bitstream` setting"
#: a command that replaces by name, or deletes where it runs: only ever in the flow's run directory
IN_THE_RUN_DIRECTORY = "inside the flow's run directory, which is xeda's"
IN_MEMORY = "replaces a module of the design in memory with a cell of a Liberty file: no file"

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
        "flows/yosys/templates/yosys_liberty_synth.ys",
        "read_liberty -overwrite -setattr liberty_cell -lib {{lib|read_path}}",
    ): (1, IN_MEMORY, None),
    (
        "flows/yosys/templates/yosys_liberty_synth.ys",
        "read_liberty -overwrite -setattr liberty_cell -lib {{asic.dff_liberty|read_path}}",
    ): (1, IN_MEMORY, None),
    (
        "flows/yosys/templates/yosys_liberty_synth.ys",
        "read_liberty -overwrite -setattr liberty_cell -unit_delay -wb -ignore_miss_func "
        "-ignore_buses {{lib|read_path}}",
    ): (1, IN_MEMORY, None),
    (
        "flows/yosys/templates/yosys_liberty_synth.ys",
        "read_liberty -overwrite -setattr liberty_cell -unit_delay -wb -ignore_miss_func "
        "-ignore_buses {{asic.dff_liberty|read_path}}",
    ): (1, IN_MEMORY, None),
    (
        "flows/yosys/templates/yosys_liberty_synth.tcl",
        "yosys read_liberty -overwrite -setattr liberty_cell -lib {{lib|read_path}}",
    ): (1, IN_MEMORY, None),
    (
        "flows/yosys/templates/yosys_liberty_synth.tcl",
        "yosys read_liberty -overwrite -setattr liberty_cell -lib {{asic.dff_liberty|read_path}}",
    ): (1, IN_MEMORY, None),
    (
        "flows/yosys/templates/yosys_liberty_synth.tcl",
        "yosys read_liberty -overwrite -setattr liberty_cell -unit_delay -wb -ignore_miss_func "
        "-ignore_buses {{lib|read_path}}",
    ): (1, IN_MEMORY, None),
    (
        "flows/yosys/templates/yosys_liberty_synth.tcl",
        "yosys read_liberty -overwrite -setattr liberty_cell -unit_delay -wb -ignore_miss_func "
        "-ignore_buses {{asic.dff_liberty|read_path}}",
    ): (1, IN_MEMORY, None),
    (
        "flows/vivado/templates/vivado_alt_synth.tcl",
        "write_checkpoint -force ${checkpoints_dir}/post_synth",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/implementation.tcl",
        "write_checkpoint -force ${checkpoints_dir}/post_place",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/implementation.tcl",
        "write_checkpoint -force ${checkpoints_dir}/post_route",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/vivado_alt_synth.tcl",
        "report_utilization -hierarchical -force -file ${reports_dir}/post_synth/hierarchical_utilization.rpt",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/implementation.tcl",
        "report_utilization -hierarchical -force -file ${reports_dir}/post_place/hierarchical_utilization.rpt",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/implementation.tcl",
        "write_verilog -mode funcsim -force ${settings.outputs_dir}/impl_funcsim.v",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/implementation.tcl",
        "write_sdf -mode timesim -process_corner slow -force -file ${settings.outputs_dir}/impl_timesim.sdf",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/implementation.tcl",
        "write_verilog -mode timesim -sdf_anno false -force -file ${settings.outputs_dir}/impl_timesim.v",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/implementation.tcl",
        "##    write_vhdl    -mode funcsim -include_xilinx_libs -write_all_overrides -force -file "
        "${settings.outputs_dir}/impl_funcsim_xlib.vhd",
    ): (1, "a comment", None),
    (
        "flows/vivado/templates/implementation.tcl",
        "write_xdc -no_fixed_only -force ${settings.outputs_dir}/impl.xdc",
    ): (1, _REPORT, None),
    (
        "flows/vivado/templates/implementation.tcl",
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
    (
        "remote/bootstrap.sh",
        'rm -rf "$XEDA_BOOT_BREAKER"',
    ): (
        2,
        "the bootstrap script runs on the remote host, outside a run directory, in its own "
        "layout `R/v1`: the break directory of a lock, which this process made or which is older "
        "than the lock limit (its process stopped)",
        None,
    ),
    (
        "remote/bootstrap.sh",
        'rm -rf "$XEDA_BOOT_SWEEP_PATH"',
    ): (
        1,
        "the bootstrap script runs on the remote host, outside a run directory, in its own "
        "layout `R/v1`: a work, staging or uv directory whose name has this host and a process "
        "that is gone (a process that was killed)",
        None,
    ),
    (
        "remote/bootstrap.sh",
        'rm -rf "$XEDA_BOOT_ENV_BUILDING"',
    ): (
        1,
        "the bootstrap script runs on the remote host, outside a run directory, in its own "
        "layout `R/v1`: the environment directory that this bootstrap process is building, removed after a failure",
        None,
    ),
    (
        "remote/bootstrap.sh",
        'rm -rf "$XEDA_BOOT_STAGE"',
    ): (
        2,
        "the bootstrap script runs on the remote host, outside a run directory, in its own "
        "layout `R/v1`: the staging directory (`code/.<key>.<pid>`, `uv/<release>.new.<pid>`) that this bootstrap process made",
        None,
    ),
    (
        "remote/bootstrap.sh",
        'rm -rf "$XEDA_BOOT_WORK"',
    ): (
        2,
        "the bootstrap script runs on the remote host, outside a run directory, in its own "
        "layout `R/v1`: the work directory (`tmp/<pid>`) that this bootstrap process made",
        None,
    ),
    (
        "remote/bootstrap.sh",
        'rm -rf "$XEDA_BOOT_LOCKS/$XEDA_BOOT_HELD.lock"',
    ): (
        1,
        "the bootstrap script runs on the remote host, outside a run directory, in its own "
        "layout `R/v1`: a lock directory that this bootstrap process holds, released at its end",
        None,
    ),
    (
        "remote/bootstrap.sh",
        'rm -rf "$1"',
    ): (
        1,
        "the bootstrap script runs on the remote host, outside a run directory, in its own "
        "layout `R/v1`: a lock directory whose owner process is gone, broken under the break lock after the owner was read again",
        None,
    ),
    (
        "remote/bootstrap.sh",
        'rm -rf "$XEDA_BOOT_LOCKS/$1.lock"',
    ): (
        1,
        "the bootstrap script runs on the remote host, outside a run directory, in its own "
        "layout `R/v1`: a lock directory that this bootstrap process holds, released",
        None,
    ),
    (
        "remote/bootstrap.sh",
        'rm -rf "$XEDA_BOOT_UV_DIR"',
    ): (
        1,
        "the bootstrap script runs on the remote host, outside a run directory, in its own "
        "layout `R/v1`: an incomplete uv directory (no `uv` file), under the lock of that uv release",
        None,
    ),
    (
        "remote/bootstrap.sh",
        'rm -rf "$XEDA_BOOT_ENV_DIR"',
    ): (
        1,
        "the bootstrap script runs on the remote host, outside a run directory, in its own "
        "layout `R/v1`: an environment directory without a valid completion marker, under the lock of its key",
        None,
    ),
    (
        "remote/bootstrap.sh",
        'rm -rf "$XEDA_BOOT_V/code/$XEDA_BOOT_KEY"',
    ): (
        1,
        "the bootstrap script runs on the remote host, outside a run directory, in its own "
        "layout `R/v1`: a code directory without a valid completion marker, under the lock of its key",
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
    platform scripts -- and every tool command in a flow's code: each line that
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
# The static scan, for writes: every file xeda writes by name goes through
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
        "flow_runner/dependencies.py",
        "os.rename(partial, storage)",
        "a complete dependency storage, written in a temporary directory and published under its "
        "key and the exclusive lock of the storage; the path is named through `unlinked`",
    ),
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
        "`live_log`: a tool's log, a new temporary renamed onto the log's name at once, so that "
        "it can be watched while the run goes on. Whatever was at the name is replaced as a name, "
        "never written through; later lines go to the descriptor of the new file, never to the "
        "name. Tool logs only: every other file is complete, then renamed",
    ),
    (
        "utils.py",
        'with os.fdopen(fd, mode, encoding=None if "b" in mode else encoding) as f:',
        "`replacing_file`: the temporary file `mkstemp` just created",
    ),
    (
        "utils.py",
        'with os.fdopen(fd, "w", encoding=encoding, buffering=1) as f:',
        "`live_log`: the temporary file `mkstemp` just created, already at the log's name",
    ),
    (
        "utils.py",
        'fd = os.open(target, os.O_WRONLY | os.O_APPEND | getattr(os, "O_NOFOLLOW", 0))',
        "`live_log` with `append`: the log that an earlier tool of this run made at its name; "
        "`O_NOFOLLOW` makes the open fail on a link, and the descriptor is kept only for a regular "
        "file with one name",
    ),
    (
        "utils.py",
        'with os.fdopen(fd, "a", encoding=encoding, buffering=1) as f:',
        "`live_log` with `append`: the descriptor of that log, which `_open_for_append` checked",
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
        "os.replace(source, temporary)",
        "a delivery by move: the run's own file renamed over the delivery's temporary "
        "placeholder, which `mkstemp` just created beside the destination. A link at the "
        "placeholder's name is not there: `mkstemp` made it exclusively",
    ),
    (
        "deliver.py",
        "os.replace(temporary, source)",
        "the undo of a move: the file goes back to the run directory, to the name it left",
    ),
    (
        "deliver.py",
        'with os.fdopen(fd, "wb") as out:',
        "a delivery's temporary, which `mkstemp` just created beside the destination",
    ),
    (
        "flow_runner/run_lock.py",
        'return os.fdopen(descriptor, "a")',
        "the descriptor of the lock file that the line above opened: held to be locked, not written",
    ),
    (
        "flow_runner/run_lock.py",
        "descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o666)",
        "the lock file beside a run directory or a storage, in the run root: opened to be locked, "
        "not written, and never through a link",
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
        "platforms/providers.py",
        "source.rename(plan.directory)",
        "the install command writes the PDK store, outside a run: a rename of the platform "
        "directory of its own complete partial checkout to a name that the install checked to be "
        "free",
    ),
    (
        "platforms/providers.py",
        'with os.fdopen(os.open(lock_path, flags, 0o666), "a") as lock:',
        "the install command writes the PDK store, outside a run: the lock file of the store, "
        "opened to be locked, not written; `O_NOFOLLOW` makes the open fail on a link, and the "
        "install checked the way to the file for links just before",
    ),
    (
        "platforms/providers.py",
        'with os.fdopen(os.open(lock_path, flags, 0o666), "a") as lock:',
        "the install command writes the PDK store, outside a run: the lock file of the store, "
        "opened to be locked, not written; `O_NOFOLLOW` makes the open fail on a link, and the "
        "install checked the way to the file for links just before",
    ),
    (
        "remote/agent.py",
        "descriptor = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)",
        "the remote agent, outside a run directory: the lock file of a workspace, opened to be "
        "locked, not written. `O_NOFOLLOW` makes the open fail on a link, and the agent checks "
        "that the open file is a regular file and still the file at that name before it relies on "
        "the lock",
    ),
    (
        "remote/agent.py",
        "descriptor = os.open(path / name, os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK)",
        "`xeda.remote.agent clean`, outside a run directory: the lock file of a workspace, an "
        "environment or a code copy, opened to try the exclusive lock, not written (an exclusive "
        "flock on Linux NFS needs a file open for writing). It creates nothing, `O_NOFOLLOW` "
        "makes it fail on a link, and `O_NONBLOCK` keeps a FIFO from blocking it (`clean` then "
        "skips a lock that is no regular file)",
    ),
    (
        "remote/agent.py",
        "descriptor = os.open(path, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)",
        "the remote agent, outside a run directory: the lock file of an environment or a code "
        "copy of the remote root, opened to be locked, not written (it is empty, and made only "
        "when provisioning did not make it). `O_NOFOLLOW` makes the open fail on a link, "
        "`O_NONBLOCK` keeps a FIFO from blocking it, and the agent checks that the open file is "
        "a regular file and still the file at that name before it relies on the lock",
    ),
    (
        "remote/agent.py",
        "os.rename(path, hidden)",
        "`xeda.remote.agent clean`, outside a run directory: the workspace, environment or code "
        "copy whose lock `clean` holds, renamed to a hidden name before its deletion (see `REVIEWED_PY_DELETIONS`)",
    ),
    (
        "remote/agent.py",
        "descriptor = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o644)",
        "the remote agent, outside a run directory: the log of a launch, a new file in the new "
        "directory of the launch, created exclusively (an existing entry, a link included, is "
        "refused); the launch writes its output to it",
    ),
    (
        "remote/manifest.py",
        "return os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)",
        "the remote sync: a read-only descriptor on a directory; it creates and writes nothing, "
        "and `O_NOFOLLOW` makes it fail on a link",
    ),
    (
        "remote/manifest.py",
        "descriptor = os.open(temporary, flags, 0o600, dir_fd=parent)",
        "the remote sync, outside a run directory: a new temporary file in a directory reached "
        "without a link, created exclusively with a random name (an existing entry, a link "
        "included, is refused); it is checked against its SHA-256 and then renamed",
    ),
    (
        "remote/environment.py",
        'with tarfile.open(fileobj=out, mode="w", format=tarfile.GNU_FORMAT) as tar:',
        "`write_code_tar`: writes the code snapshot to the stream that it is given and names no "
        "file",
    ),
    (
        "remote/manifest.py",
        'with tarfile.open(fileobj=out, mode="w|", format=tarfile.PAX_FORMAT) as archive:',
        "`write_tar`: writes to the stream that it is given and names no file",
    ),
    (
        "remote/manifest.py",
        'archive = tarfile.open(fileobj=tar_stream, mode="r|")',
        "`iter_tar`: reads the stream that it is given and extracts nothing",
    ),
    (
        "remote/manifest.py",
        "descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)",
        "the remote sync: a read-only descriptor on a file, to compare its digest; it creates and "
        "writes nothing, `O_NOFOLLOW` makes it fail on a link and `O_NONBLOCK` keeps a pipe from "
        "blocking it",
    ),
    (
        "remote/manifest.py",
        "os.replace(temporary, name, src_dir_fd=parent, dst_dir_fd=parent)",
        "the remote sync: a file that passed its SHA-256 check, renamed over its name; a link at "
        "the name is replaced, never written through",
    ),
    (
        "remote/manifest.py",
        'with os.fdopen(descriptor, "wb") as out:',
        "the remote sync: the temporary file that was just created exclusively",
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
    is removed as a link before the run, never what it leads to; and `--outputs-to` skips a link
    that leads out, to a file or a directory, and refuses one that leads nowhere with a
    `DeliveryError` naming it, while a link inside delivers what it leads to."""
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

        results_description: dict[str, MetricSpec] = {}

        class Settings(Flow.Settings):
            work_dir: Path = Field(Path("work"), description="its work.", json_schema_extra=WORKING)

        def run(self) -> None:
            run_dir = self.run_path
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

    design = Design(name="links", root=tmp_path, rtl={"sources": [str(SQRT / "sqrt.vhdl")]})
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
        # deliveries: a link inside delivers what it leads to; a link out, to a file or a
        # directory, is skipped
        delivered = tmp_path / "delivered"
        deliver.update(a="link_in_file", b="link_out_file", c="link_in_dir", d="link_out_dir")
        runner = DefaultRunner(root, display_results=False, rebuild_all=True, outputs_to=delivered)
        assert runner.launch_flow(LinkLeaver, design, {}).succeeded
        assert (delivered / "link_in_file").read_text() == "inside\n"
        assert not os.path.lexists(delivered / "link_out_file")
        assert (delivered / "link_in_dir" / "f.txt").read_text() == "in sub\n"
        assert not os.path.lexists(delivered / "link_out_dir")
        for artifact in ("dangling", "cycle_a"):
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
        assert scrub_runs(
            LinkLeaver.name, run_dir.parent, run_root=root.resolve(), confirm=lambda _: True
        )
        assert not run_dir.exists()
        assert _outside_tree(outside) == before
    finally:
        for name in (LinkLeaver.name, LinkLeaver.__name__):
            registered_flows.pop(name, None)


# ---------------------------------------------------------------- the private settings files

#: The private files that the audit hook watches, once a test sets it: the list collects the
#: files opened.
_PRIVATE_OPENS: Optional[list] = None
_PRIVATE_ROOT: Optional[Path] = None


def _audit_private_files(event: str, args: tuple) -> None:
    if _PRIVATE_OPENS is None or event != "open" or not isinstance(args[0], (str, os.PathLike)):
        return
    path = Path(os.fsdecode(args[0]))
    if _PRIVATE_ROOT is not None and path.is_relative_to(_PRIVATE_ROOT):
        if path.name == "config.yaml" or path.name.endswith(".local.yaml"):
            _PRIVATE_OPENS.append(path)


sys.addaudithook(_audit_private_files)


def test_only_local_settings_and_the_command_line_open_the_private_settings_files(
    tmp_path, monkeypatch
):
    """A planning or a launch opens neither `config.yaml` nor `*.local.yaml`: the library is
    handed what they hold. `local_settings()` and the command line read them."""
    global _PRIVATE_OPENS, _PRIVATE_ROOT
    from xeda.local_settings import local_settings
    from xeda.project import Project

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "cfg"))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    (tmp_path / "cfg" / "xeda").mkdir(parents=True)
    (tmp_path / "cfg" / "xeda" / "config.yaml").write_text("run_root: user_runs\n")
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "knight.v").write_text("module knight(); endmodule\n")
    project_file = repo / "xedaproject.yaml"
    project_file.write_text(
        yaml.safe_dump(
            {"designs": [{"name": "d", "rtl": {"sources": ["knight.v"], "top": "knight"}}]}
        )
    )
    (repo / "xedaproject.local.yaml").write_text("run_root: local_runs\n")
    settings = ["fpga.part=xc7a100tcsg324-1"]
    _PRIVATE_ROOT, _PRIVATE_OPENS = tmp_path, []
    try:
        project = Project.load(project_file)
        design = project.design("d")
        plan = DefaultRunner(tmp_path / "run").plan(
            "vivado_synth", design, flow_settings=node_settings("vivado_synth", settings)
        )
        assert plan.requested == "vivado_synth"
        assert _PRIVATE_OPENS == [], "the library read a private file"
        local_settings(project)
        assert {path.name for path in _PRIVATE_OPENS} == {"config.yaml", "xedaproject.local.yaml"}
        _PRIVATE_OPENS.clear()
        result = CliRunner().invoke(
            cli,
            ["run", "vivado_synth", "--xedaproject", str(project_file), "--design-name", "d",
             "--dry-run", "-s", f"vivado_synth.{settings[0]}", "--json"],
        )  # fmt: skip
        assert result.exit_code == 0, result.output
        assert {path.name for path in _PRIVATE_OPENS} == {"config.yaml", "xedaproject.local.yaml"}
        _PRIVATE_OPENS.clear()
        result = CliRunner().invoke(
            cli,
            ["run", "vivado_synth", "--xedaproject", str(project_file), "--design-name", "d",
             "--dry-run", "--no-config", "-s", f"vivado_synth.{settings[0]}", "--json"],
        )  # fmt: skip
        assert result.exit_code == 0, result.output
        assert _PRIVATE_OPENS == [], "--no-config read a private file"
    finally:
        _PRIVATE_OPENS = _PRIVATE_ROOT = None
