"""The artifact contract is the same for transfer, path rewriting and cleanup."""

import json
import logging
import os
import pickle
import time
from copy import deepcopy
from pathlib import Path
from typing import ClassVar

import pytest
from box import Box

from xeda import Design
from xeda.artifacts import filter_artifact_paths, iter_artifact_paths, map_artifact_paths
from xeda.flow import Flow, registered_flows
from xeda.flow.run_dir import RUN_DIR_MARKER
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.default_runner import _artifact_rows

from .tool_utils import use_fake_tools

EXAMPLE = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt" / "sqrt.toml"


@pytest.mark.parametrize("as_box", [False, True])
def test_nested_artifacts_preserve_labels_order_shape_and_optional_values(as_box):
    artifacts = {
        "label_not_a_path": "netlist.json",
        "generated": [Path("a.v"), {"more": ("b.v", "netlist.json")}],
        "optional": [None, "", False, 7, [], {}],
    }
    if as_box:
        artifacts = Box(artifacts)
    before = deepcopy(artifacts)

    assert list(iter_artifact_paths(artifacts)) == [
        "netlist.json",
        Path("a.v"),
        "b.v",
        "netlist.json",
    ]
    rewritten = map_artifact_paths(artifacts, lambda path: str(Path("local") / path))
    assert rewritten == {
        "label_not_a_path": "local/netlist.json",
        "generated": ["local/a.v", {"more": ("local/b.v", "local/netlist.json")}],
        "optional": [None, "", False, 7, [], {}],
    }
    assert dict(artifacts) == before


@pytest.mark.parametrize("artifact", ["netlist.json", Path("netlist.json")])
def test_an_artifact_can_be_a_single_path(artifact):
    assert list(iter_artifact_paths(artifact)) == [artifact]
    assert (
        map_artifact_paths(artifact, lambda path: str(Path("local") / path)) == "local/netlist.json"
    )


def test_artifact_rows_show_every_path_once_under_its_label():
    """Each path gets a row in the path column, its label only on the first row of its group."""
    artifacts = {
        "bitstream": Path("top.bit"),
        "netlists": [Path("a.v"), Path("b.v")],
        "reports": ("timing.rpt", "utilization.rpt"),
        "logs": Box({"synth": {"stdout": "synth.log", "stderr": "synth.err"}}),
        "unused": [None, "", {}, []],
    }

    assert _artifact_rows(artifacts) == [
        ("bitstream", "top.bit", True),
        ("netlists", "a.v", False),
        ("", "b.v", True),
        ("reports", "timing.rpt", False),
        ("", "utilization.rpt", True),
        ("logs", "synth.log", False),
        ("", "synth.err", True),
    ]


def test_filtering_drops_rejected_paths_and_the_groups_they_emptied():
    """A rejected path leaves its group; a group whose every path was rejected leaves its
    parent, while one that never held a path stays as it was."""
    artifacts = Box(
        {
            "netlist": "gone.v",
            "generated": [Path("a.v"), Path("gone_a.v"), {"more": ("gone_b.v",)}],
            "optional": [None, "", {}],
            "kept": {"sdf": "impl.sdf", "sdc": "gone.sdc"},
        }
    )
    before = deepcopy(artifacts)

    filtered = filter_artifact_paths(artifacts, lambda path: "gone" not in str(path))

    assert filtered == {
        "generated": [Path("a.v")],
        "optional": [None, "", {}],
        "kept": {"sdf": "impl.sdf"},
    }
    assert artifacts == before


@pytest.mark.parametrize("passes", [True, False], ids=["succeeded", "failed"])
def test_a_failed_run_reports_only_the_artifacts_that_exist(passes, tmp_path, caplog):
    """A flow records its outputs before the tool runs. A failed run may not have written them,
    and its results listed them anyway: the remote runner then fetched a file that was not
    there, and a failed remote run crashed instead of reporting its failure. A successful run
    reports what it recorded."""

    class HalfWritten(Flow):
        """Record four outputs, write two of them, and succeed or fail as the test says."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self) -> None:
            (self.run_path / "written.v").write_text("module m; endmodule\n")
            (self.run_path / "outputs").mkdir()
            (self.run_path / "outputs" / "written.dcp").write_text("")
            self.artifacts.netlist = "written.v"
            self.artifacts.sdf = "never_written.sdf"
            self.artifacts.checkpoints = [
                self.run_path / "outputs" / "written.dcp",
                self.run_path / "outputs" / "never_written.dcp",
            ]
            self.results.artifacts["sdc"] = "never_written.sdc"

        def parse_reports(self) -> bool:
            return passes

    design = Design.from_file(EXAMPLE)
    try:
        with caplog.at_level(logging.WARNING):
            flow = DefaultRunner(tmp_path / "run", display_results=False).launch_flow(
                HalfWritten, design, {}
            )
    finally:
        for name in (HalfWritten.name, HalfWritten.__name__):
            registered_flows.pop(name, None)

    assert flow.succeeded == passes
    written_dcp = flow.run_path / "outputs" / "written.dcp"
    missing_dcp = flow.run_path / "outputs" / "never_written.dcp"
    expected = {"netlist": "written.v", "checkpoints": [str(written_dcp)]}
    if passes:
        expected["checkpoints"].append(str(missing_dcp))
        expected |= {"sdf": "never_written.sdf", "sdc": "never_written.sdc"}
    saved = json.loads((flow.run_path / "results.json").read_text())
    assert saved["success"] == passes
    assert saved["artifacts"] == expected
    assert json.loads(json.dumps(flow.results.artifacts, default=str)) == expected

    warnings = [r.getMessage() for r in caplog.records if r.levelno == logging.WARNING]
    dropped = [w for w in warnings if "never_written" in w]
    if passes:
        assert not dropped
    else:
        (warning,) = dropped
        assert warning.startswith("half_written failed")
        for dropped in (
            "sdf: never_written.sdf",
            f"checkpoints: {missing_dcp}",
            "sdc: never_written.sdc",
        ):
            assert dropped in warning


#: The flows the fake tools run: the settings each needs (`clean` off, so an earlier run's outputs
#: are still there when the next run starts), and the tool command that fails in the script that
#: writes its outputs (`XEDA_FAKE_TOOL_FAIL`), after the flow has declared them.
FAKE_TOOL_FLOWS = {
    "vivado_synth": (
        {"fpga": "xc7a12tcsg325-1", "clock_period": 10.0, "bitstream": "o/top.bit"},
        "launch_runs",
    ),
    "vivado_alt_synth": (
        {"fpga": "xc7a12tcsg325-1", "clock_period": 10.0, "write_netlist": True},
        "synth_design",
    ),
    # the first command of `compile.tcl` (the fake records `qexit -error` rather than exiting)
    "quartus": ({"fpga": "10CL016YU256C6G", "clock_period": 10.0}, "load_package"),
    "ise_synth": ({"fpga": "xc6slx9-2-tqg144", "clock_period": 10.0}, "{Implement Design}"),
    "diamond_synth": ({"fpga": "LFE5U-25F-6BG256C", "clock_period": 10.0}, "prj_run"),
    "dc": ({"target_libraries": ["cells.db"], "clock_period": 10.0}, "elaborate"),
    "vivado_sim": ({"vcd": "wave.vcd", "saif": "power.saif"}, "xvhdl"),
}


def _sqrt_with_a_vhdl_testbench(root: Path) -> Design:
    """`sqrt` with a plain VHDL testbench, which the Vivado simulator can run (its own is a
    cocotb one)."""
    root.mkdir()
    (root / "tb.vhd").write_text("entity tb is end;\narchitecture sim of tb is begin end;\n")
    return Design(
        name="sqrt",
        design_root=root,
        rtl={"sources": [str(EXAMPLE.parent / "sqrt.vhdl")], "top": "sqrt"},
        tb={"sources": ["tb.vhd"], "top": "tb", "uut": "uut"},
        language={"vhdl": {"standard": "2008"}},
    )


@pytest.mark.parametrize("flow_name", sorted(FAKE_TOOL_FLOWS))
def test_a_failed_run_never_reports_an_earlier_runs_artifact(flow_name, tmp_path, monkeypatch):
    """Every artifact a flow declares is there from an earlier run; the next run's tool fails
    before writing any of them. The failed run's `results.json` lists none of those files: a
    file counts as the run's own only if it changed from the state recorded of it before the
    run, whichever flow wrote it -- no flow has to remember to clear its outputs."""
    if flow_name == "vivado_sim":
        design = _sqrt_with_a_vhdl_testbench(tmp_path / "design")
    else:
        design = Design.from_file(EXAMPLE)
    (tmp_path / "cells.db").write_text("")
    monkeypatch.chdir(tmp_path)
    settings, failing = FAKE_TOOL_FLOWS[flow_name]
    settings = settings | {"clean": False}
    runner = DefaultRunner(  # as `xeda run` makes it: one run directory, reused
        tmp_path / "run", display_results=False, cached_dependencies=False, incremental=True
    )
    use_fake_tools(monkeypatch)
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", failing)
    declared = runner.launch_flow(flow_name, design, settings)
    run_dir = declared.run_path
    # every declared output, and every other file in the run directory, as an earlier run left it
    for path in iter_artifact_paths(declared.artifacts):
        path = Path(os.path.abspath(run_dir / path))
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_text("an earlier run's\n")
    earlier = {path for path in run_dir.rglob("*") if path.is_file()}
    for path in earlier:
        os.utime(path, ns=(EARLIER_NS, EARLIER_NS))

    again = runner.launch_flow(flow_name, design, settings)

    assert not again.succeeded
    saved = json.loads((again.run_path / "results.json").read_text())
    listed = [Path(os.path.abspath(run_dir / p)) for p in iter_artifact_paths(saved["artifacts"])]
    stale = [p for p in listed if p.is_file() and p.stat().st_mtime_ns == EARLIER_NS]
    assert not stale, [str(p) for p in stale]


#: The modification time an earlier run's files are given: 1970, long before any run starts.
EARLIER_NS = 1_000_000_000


# ---------------------------------------------------------------------------------------------
# Freshness from each output's own prior state, never a clock (E17 part 2)
# ---------------------------------------------------------------------------------------------


def test_a_new_output_with_an_early_mtime_still_counts_as_written(tmp_path):
    """(a) A file this run creates counts as written even when its mtime is set earlier than the
    run started -- simulating a copy or a tool that preserves an old timestamp, or a file system
    whose clock is behind. The run-directory snapshot `Flow.__init__` takes before `run()` found
    nothing at this path, so its prior state is "absent": whatever timestamp the file ends up
    with, existing at all is a change from that, never mind what either clock reads."""

    class NewOutputEarlyMtime(Flow):
        """Write one output, then set its mtime long before this run could have started."""

        results_description: ClassVar[dict] = {}

        def run(self) -> None:
            path = self.run_path / "fresh.bit"
            path.write_text("fresh\n")
            os.utime(path, ns=(EARLIER_NS, EARLIER_NS))
            self.artifacts.bitstream = "fresh.bit"

        def parse_reports(self) -> bool:
            return False  # force `_drop_unwritten_artifacts`, which consults `wrote_output`

    design = Design.from_file(EXAMPLE)
    try:
        flow = DefaultRunner(tmp_path / "run", display_results=False).launch_flow(
            NewOutputEarlyMtime, design, {}
        )
    finally:
        for name in (NewOutputEarlyMtime.name, NewOutputEarlyMtime.__name__):
            registered_flows.pop(name, None)

    assert not flow.succeeded
    saved = json.loads((flow.run_path / "results.json").read_text())
    assert saved["artifacts"] == {"bitstream": "fresh.bit"}


def test_an_untouched_artifact_at_the_run_starts_own_tick_is_not_written(tmp_path):
    """(b) An earlier run's artifact, never touched by this one, is not counted as written even
    when its mtime happens to fall in the exact tick this run started in -- the coarse-clock case
    (FAT's 2s) a `mtime >= run_start` comparison could get wrong. Comparison is by the file's own
    recorded state (its snapshot, taken at construction, unchanged since), never by reading any
    clock at all, so the coincidence cannot matter."""
    run_dir = tmp_path / "run"
    run_dir.mkdir()
    (run_dir / RUN_DIR_MARKER).write_text("format = 1\n")  # so the launcher may reuse it as is
    old = run_dir / "old.bit"
    old.write_text("old\n")
    now = time.time()
    os.utime(old, (now, now))  # the same tick a coarse clock would call "run start"

    class SeesEarlierArtifact(Flow):
        """Declare an artifact an earlier run left, without ever touching the file."""

        results_description: ClassVar[dict] = {}

        def run(self) -> None:
            self.artifacts.bitstream = "old.bit"

        def parse_reports(self) -> bool:
            return False

    design = Design.from_file(EXAMPLE)
    try:
        flow = DefaultRunner(tmp_path / "xeda_run", display_results=False).launch_flow(
            SeesEarlierArtifact, design, {}, run_path=run_dir
        )
    finally:
        for name in (SeesEarlierArtifact.name, SeesEarlierArtifact.__name__):
            registered_flows.pop(name, None)

    assert not flow.succeeded
    saved = json.loads((flow.run_path / "results.json").read_text())
    assert saved.get("artifacts", {}) == {}


def test_a_successful_vivado_synth_writes_an_external_bitstream_with_an_early_mtime(
    tmp_path, monkeypatch
):
    """(c) sol/luna's scenario: an external bitstream (outside the run directory, so possibly on
    another file system) already exists with an old timestamp -- as if that file system's clock
    were behind, or an earlier run left it there. `VivadoSynth.run` records its prior state
    (`remove_stale_output`) before Vivado can touch it; once Vivado's `write_bitstream` step
    genuinely overwrites it, the check at the end of `run()` compares identity and metadata, never
    a clock, so a successful build never raises `FlowFatalError` for a bitstream it plainly
    wrote."""
    use_fake_tools(monkeypatch)
    design = Design.from_file(EXAMPLE)
    external = tmp_path / "external"
    external.mkdir()
    bitstream = external / "top.bit"
    bitstream.write_text("an earlier run's, on a lagging clock\n")
    os.utime(bitstream, ns=(EARLIER_NS, EARLIER_NS))
    runner = DefaultRunner(tmp_path / "run", display_results=False, cached_dependencies=False)

    flow = runner.launch_flow(
        "vivado_synth",
        design,
        {"fpga": "xc7a12tcsg325-1", "clock_period": 10.0, "bitstream": str(bitstream)},
    )

    assert flow.succeeded
    assert bitstream.read_text() != "an earlier run's, on a lagging clock\n"
    assert bitstream.stat().st_mtime_ns != EARLIER_NS


def test_a_directly_constructed_flow_does_not_reintroduce_the_stale_artifact_bug(tmp_path):
    """The invariant behind this whole mechanism: whichever way a flow reaches `run()` -- through
    the launcher, or constructed directly, as a flow built for another purpose might do -- its run
    directory's prior state is already known by the time anything can write to it, because
    `Flow.__init__` takes the snapshot itself, and construction is the one thing that must happen
    before `run()` can be called at all. So a directly-constructed flow, never touched by the
    launcher's own bookkeeping, still tells an earlier run's untouched artifact from a fresh one:
    an unrecorded path never silently falls back to "unknown -- not written" just because nothing
    launched it."""

    class DirectlyConstructed(Flow):
        results_description: ClassVar[dict] = {}

        def run(self) -> None:
            pass

    run_dir = tmp_path / "run"
    run_dir.mkdir()
    old = run_dir / "old.bit"
    old.write_text("old\n")  # here before the flow is even constructed

    try:
        flow = DirectlyConstructed({}, Design.from_file(EXAMPLE), run_dir)
        # created only after construction: absent from the snapshot taken at that point
        fresh = run_dir / "fresh.bit"
        fresh.write_text("fresh\n")

        assert flow.wrote_output("old.bit") is False
        assert flow.wrote_output("fresh.bit") is True
    finally:
        for name in (DirectlyConstructed.name, DirectlyConstructed.__name__):
            registered_flows.pop(name, None)


def test_a_directly_constructed_flow_with_a_relative_run_path_is_still_sound(tmp_path, monkeypatch):
    """The same invariant, but with a *relative* `run_path` (luna's finding): `Flow.__init__`
    must snapshot and `wrote_output` must look paths up under the *same* normalized (absolute)
    form of the run directory, or an untouched earlier artifact is missed at snapshot time --
    recorded under a relative key -- and then reported as written, because looking it up later
    (always through an absolute path) finds nothing there and falls back to "absent"."""
    monkeypatch.chdir(tmp_path)

    class DirectlyConstructedRelative(Flow):
        results_description: ClassVar[dict] = {}

        def run(self) -> None:
            pass

    run_dir = Path("run")  # relative to the (now current) tmp_path
    (tmp_path / run_dir).mkdir()
    old = tmp_path / run_dir / "old.bit"
    old.write_text("old\n")  # here before the flow is even constructed

    try:
        flow = DirectlyConstructedRelative({}, Design.from_file(EXAMPLE), run_dir)
        fresh = tmp_path / run_dir / "fresh.bit"
        fresh.write_text("fresh\n")

        assert flow.wrote_output("old.bit") is False
        assert flow.wrote_output("fresh.bit") is True
    finally:
        for name in (DirectlyConstructedRelative.name, DirectlyConstructedRelative.__name__):
            registered_flows.pop(name, None)


# ---------------------------------------------------------------------------------------------
# An earlier output is the same output under any name: the snapshot is keyed by identity
# ---------------------------------------------------------------------------------------------


def _earlier_run_dir(tmp_path: Path) -> Path:
    """A run directory an earlier run left: marked, so the launcher reuses it as it is, with
    `old.bit` and `real/old.bit` from that run."""
    run_dir = tmp_path / "run"
    (run_dir / "real").mkdir(parents=True)
    (run_dir / RUN_DIR_MARKER).write_text("format = 1\n")
    for old in (run_dir / "old.bit", run_dir / "real" / "old.bit"):
        old.write_text("an earlier run's\n")
        os.utime(old, ns=(EARLIER_NS, EARLIER_NS))
    return run_dir


def _failed_run_artifacts(tmp_path: Path, run_dir: Path, artifact: str, rewrite=None) -> dict:
    """The artifacts a failed run into `run_dir` reports when it declares `artifact`, after
    writing `rewrite` (a file of the run directory) if one is given."""

    class DeclaresAnOutput(Flow):
        """Declare one output, write it only if told to, and fail."""

        results_description: ClassVar[dict] = {}

        def run(self) -> None:
            self.artifacts.output = artifact
            if rewrite is not None:
                rewrite.write_text("this run's, and longer\n")

        def parse_reports(self) -> bool:
            return False

    try:
        flow = DefaultRunner(tmp_path / "xeda_run", display_results=False).launch_flow(
            DeclaresAnOutput, Design.from_file(EXAMPLE), {}, run_path=run_dir
        )
    finally:
        for name in (DeclaresAnOutput.name, DeclaresAnOutput.__name__):
            registered_flows.pop(name, None)
    assert not flow.succeeded
    return json.loads((run_dir / "results.json").read_text()).get("artifacts", {})


#: Ways to name an earlier output other than as the run-directory walk spells it: the name the
#: flow declares, and the file of the run directory it is.
OTHER_NAMES = {
    # a link inside the run directory to a directory of it
    "linked subdirectory": (lambda tmp, run: "alias/old.bit", "real/old.bit"),
    # a link outside the run directory to a file in it (`-s vcd=/elsewhere/latest.vcd`)
    "link into the run directory": (lambda tmp, run: str(tmp / "latest.bit"), "old.bit"),
    # a path through a link to the run directory itself (a "latest" link)
    "linked run directory": (lambda tmp, run: str(tmp / "latest" / "old.bit"), "old.bit"),
    # another case of the name, on a case-insensitive file system (macOS's default)
    "letter case": (lambda tmp, run: "OLD.BIT", "old.bit"),
}


@pytest.mark.parametrize("written", [False, True], ids=["untouched", "rewritten"])
@pytest.mark.parametrize("spelling", sorted(OTHER_NAMES))
def test_an_earlier_output_under_another_name_is_still_the_earlier_output(
    spelling, written, tmp_path
):
    """A failed run lists an output it declares only if the run wrote it, however the flow names
    it: through a link into or inside the run directory, or in another letter case on a
    case-insensitive file system. The snapshot `Flow.__init__` takes is keyed by each file's
    identity (device and inode), not by how a walk of the run directory happened to spell its
    path, so another name for an earlier run's file finds that file's recorded state -- it is
    never taken for a path the snapshot did not see, and so for a file this run created."""
    run_dir = _earlier_run_dir(tmp_path)
    name_of, file = OTHER_NAMES[spelling]
    (run_dir / "alias").symlink_to(run_dir / "real", target_is_directory=True)
    (tmp_path / "latest.bit").symlink_to(run_dir / "old.bit")
    (tmp_path / "latest").symlink_to(run_dir, target_is_directory=True)
    if spelling == "letter case" and not (run_dir / "OLD.BIT").exists():
        pytest.skip("the file system is case-sensitive: OLD.BIT is another file")
    artifact = name_of(tmp_path, run_dir)

    listed = _failed_run_artifacts(
        tmp_path, run_dir, artifact, rewrite=run_dir / file if written else None
    )

    assert listed == ({"output": artifact} if written else {})


@pytest.mark.parametrize("written", [False, True], ids=["untouched", "written into"])
def test_a_directory_output_is_the_runs_only_if_the_run_changed_it(written, tmp_path):
    """An output may be a directory. The snapshot records directories as well as files, so an
    earlier run's directory this run left alone is not its own -- one it wrote a file into is,
    as a directory's own state changes when an entry is added to it."""
    run_dir = _earlier_run_dir(tmp_path)

    listed = _failed_run_artifacts(
        tmp_path, run_dir, "real", rewrite=run_dir / "real" / "new.v" if written else None
    )

    assert listed == ({"output": "real"} if written else {})


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs POSIX permissions")
def test_a_directory_the_snapshot_could_not_read_is_never_taken_for_empty(tmp_path):
    """The snapshot proves a path absent only where it could look. A directory of the run
    directory it could not list is not recorded as empty: whatever is found there later has no
    known prior state, so it is not proof the run wrote it (a failed run omits it)."""

    class NeverRuns(Flow):
        """Only constructed."""

        results_description: ClassVar[dict] = {}

        def run(self) -> None:
            pass

    run_dir = _earlier_run_dir(tmp_path)
    locked = run_dir / "real"
    locked.chmod(0)
    try:
        flow = NeverRuns({}, Design.from_file(EXAMPLE), run_dir)
    finally:
        locked.chmod(0o755)
        for name in (NeverRuns.name, NeverRuns.__name__):
            registered_flows.pop(name, None)

    assert flow.wrote_output("real/old.bit") is False
    assert flow.wrote_output("old.bit") is False


def test_a_failed_vivado_sim_does_not_list_an_earlier_vcd_named_through_a_link(
    tmp_path, monkeypatch
):
    """The case above through a real flow: `vivado_sim` names its VCD where the setting says,
    here through a link to its own run directory, and registers it before Vivado runs. A run
    whose tool fails before writing it does not list the earlier run's VCD there as its own."""
    design = _sqrt_with_a_vhdl_testbench(tmp_path / "design")
    monkeypatch.chdir(tmp_path)
    use_fake_tools(monkeypatch)
    runner = DefaultRunner(  # as `xeda run` makes it: one run directory, reused
        tmp_path / "xeda_run", display_results=False, cached_dependencies=False, incremental=True
    )
    run_dir = runner.launch_flow("vivado_sim", design, {"vcd": "wave.vcd", "clean": False}).run_path
    vcd = run_dir / "wave.vcd"
    vcd.write_text("an earlier run's\n")
    os.utime(vcd, ns=(EARLIER_NS, EARLIER_NS))
    (tmp_path / "latest").symlink_to(run_dir, target_is_directory=True)
    monkeypatch.setenv("XEDA_FAKE_TOOL_FAIL", "xvhdl")

    again = runner.launch_flow(
        "vivado_sim", design, {"vcd": str(tmp_path / "latest" / "wave.vcd"), "clean": False}
    )

    assert again.run_path == run_dir and not again.succeeded
    assert vcd.stat().st_mtime_ns == EARLIER_NS  # the earlier run's, untouched
    saved = json.loads((run_dir / "results.json").read_text())
    assert "vcd" not in saved.get("artifacts", {})


#: How far behind the run directory's clock the "other file system" of the test below runs.
LAG_NS = 3600 * 10**9


def _lagging(st: os.stat_result) -> os.stat_result:
    """`st` as a file system whose clock runs `LAG_NS` behind would report it."""
    lag_s = LAG_NS // 10**9
    fields = list(st)
    fields[8] -= lag_s  # st_mtime (whole seconds)
    fields[9] -= lag_s  # st_ctime
    return os.stat_result(
        fields,
        {
            "st_atime": st.st_atime,
            "st_mtime": st.st_mtime - lag_s,
            "st_ctime": st.st_ctime - lag_s,
            "st_atime_ns": st.st_atime_ns,
            "st_mtime_ns": st.st_mtime_ns - LAG_NS,
            "st_ctime_ns": st.st_ctime_ns - LAG_NS,
        },
    )


def test_a_successful_vivado_synth_accepts_its_bitstream_on_a_file_system_whose_clock_is_behind(
    tmp_path, monkeypatch
):
    """sol/luna's scenario itself: the bitstream is named on another file system, whose clock
    runs an hour behind the run directory's -- every time read there (mtime and ctime alike,
    which no `os.utime` can set) is an hour earlier. A run that compared those times with a
    start time read from the run directory's clock judged the bitstream Vivado had just written
    to be from before the run, and failed a correct build (`FlowFatalError`). Judged by the
    file's own state before and after, it is the run's."""
    use_fake_tools(monkeypatch)
    external = tmp_path / "external"
    external.mkdir()
    bitstream = external / "top.bit"
    bitstream.write_text("an earlier run's\n")
    real_stat = os.stat

    def stat(path, *args, **kwargs):
        st = real_stat(path, *args, **kwargs)
        inside = Path(os.fspath(path)).is_relative_to(external) if path is not None else False
        return _lagging(st) if inside else st

    monkeypatch.setattr(os, "stat", stat)
    runner = DefaultRunner(tmp_path / "run", display_results=False, cached_dependencies=False)

    flow = runner.launch_flow(
        "vivado_synth",
        Design.from_file(EXAMPLE),
        {"fpga": "xc7a12tcsg325-1", "clock_period": 10.0, "bitstream": str(bitstream)},
    )

    assert flow.succeeded
    assert bitstream.read_text() != "an earlier run's\n"


def test_a_runs_artifacts_and_results_pickle(tmp_path):
    """A flow's results cross a process boundary by pickle -- the DSE's workers return them --
    and so may its artifacts, in every shape an artifact may take: a path, a list of them, a
    mapping, a list of mappings. Recording each output's state as it is stored is the live
    flow's business, and none of it goes with them."""

    class ManyShapes(Flow):
        """Declare outputs in every shape."""

        results_description: ClassVar[dict] = {}

        def run(self) -> None:
            for name in ("a.v", "b.v", "t.rpt"):
                (self.run_path / name).write_text(name)
            self.artifacts.netlist = "a.v"
            self.artifacts.sources = ["a.v", "b.v"]
            self.artifacts.reports = {"timing": "t.rpt"}
            self.artifacts.runs = [{"netlist": "a.v", "report": "t.rpt"}]

    flow = _launch_and_forget(ManyShapes, tmp_path)

    assert flow.succeeded
    expected = {
        "netlist": "a.v",
        "sources": ["a.v", "b.v"],
        "reports": {"timing": "t.rpt"},
        "runs": [{"netlist": "a.v", "report": "t.rpt"}],
    }
    assert pickle.loads(pickle.dumps(flow.results)).artifacts == expected
    assert pickle.loads(pickle.dumps(flow.artifacts)) == expected


def _launch_and_forget(flow_class, tmp_path: Path) -> Flow:
    """Launch a flow class defined by a test, then unregister it."""
    try:
        return DefaultRunner(tmp_path / "xeda_run", display_results=False).launch_flow(
            flow_class, Design.from_file(EXAMPLE), {}
        )
    finally:
        for name in (flow_class.name, flow_class.__name__):
            registered_flows.pop(name, None)
