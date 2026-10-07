"""`xeda scrub` beside per-target run directories.

A run directory lies in `<run root>/<design>[/<target>]/<flow>[_<hash>]`, so a scrub of one flow
of one design has three honest readings: everything (`xeda scrub FLOW DESIGN`: the pre-target
`<design>/<flow>` runs and every target's), or one target's alone (`--target NAME`), and it never
reads a design file: it finds what is on disk, one level below the design for targets, and
never goes into a run directory. The oracles:

- exact candidate sets, collected before one confirmation, removed under each directory's lock;
- a target's scrub leaves the pre-target runs and every other target alone, and an unqualified
  one takes them all, a target no design file names included;
- a refused name touches nothing, and so does a link that leads out of the run root;
- a held read lease blocks the scrub until released, and what is at each path is judged again
  after it, by what it is then and not by whether it is the directory that was listed;
- the `--scrub` of a launch cannot reach another target.
"""

import json
import logging
import os
import queue
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from click.testing import CliRunner

from xeda import Design
from xeda.cli import cli
from xeda.console import console
from xeda.flow import registered_flows
from xeda.flow_runner import DefaultRunner, default_runner, run_lock
from xeda.flow_runner.run_lock import run_dir_lock, run_dir_read_lock
from xeda.run_dir import RunDirectoryError
from xeda.run_root import ensure_run_root
from xeda.utils import replacing_file

from .io_flows import _Maker

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX run-directory locks")

FLOW = "vivado_synth"
#: What scrub says of a directory it keeps, after its path.
KEPT = "its run records changed after the listing (a run finished or refreshed there)"
HASHED = f"{FLOW}_0123456789abcdef"
OTHER_HASHED = f"{FLOW}_fedcba9876543210"


@pytest.fixture(autouse=True)
def private_registry():
    before = registered_flows.copy()
    yield
    registered_flows.clear()
    registered_flows.update(before)


class Prompts(list):
    """The confirmation prompts asked, answered `yes` unless `answer` says otherwise."""

    answer = "yes"


@pytest.fixture
def confirmations(monkeypatch):
    prompts = Prompts()

    def ask(prompt="", *args, **kwargs):
        prompts.append(str(prompt))
        return prompts.answer

    monkeypatch.setattr(console, "input", ask)
    return prompts


@pytest.fixture
def said(monkeypatch):
    """What scrub says: each `console.print`, as one line."""
    lines: list[str] = []
    monkeypatch.setattr(
        console, "print", lambda *args, **kwargs: lines.append(" ".join(map(str, args)))
    )
    return lines


def run_dir(path: Path) -> Path:
    """A run directory as a launch leaves one after a success: its documents, and a file of its
    own."""
    path.mkdir(parents=True)
    (path / "settings.json").write_text("{}")
    (path / "results.json").write_text("{}")
    (path / "trace.json").write_text("{}")
    (path / "out.txt").write_text("output\n")
    return path


class Tree:
    """`<run root>/d`: pre-target runs, three targets (one without runs of the flow) and a
    target no design file names, beside run directories of other flows that hold a directory
    named like the flow, and another design with a target of the same name."""

    def __init__(self, tmp_path: Path) -> None:
        root = ensure_run_root(tmp_path / "xeda_run")
        assert root is not None
        self.root = root
        self.design = root / "d"
        self.direct = [run_dir(self.design / FLOW), run_dir(self.design / HASHED)]
        self.a = [run_dir(self.design / "a" / FLOW), run_dir(self.design / "a" / HASHED)]
        self.b = [run_dir(self.design / "b" / FLOW), run_dir(self.design / "b" / OTHER_HASHED)]
        self.ghost = [run_dir(self.design / "ghost" / FLOW)]
        # what a scrub of the flow must never remove
        self.keep = [
            run_dir(self.design / "a" / "yosys_fpga"),  # another flow, in the target
            run_dir(self.design / "a" / f"{FLOW}_other"),  # not a hash suffix
            run_dir(self.design / "yosys_fpga"),  # another flow's run directory, whose own
            run_dir(self.design / "open_xc7"),  # ... and a removed flow's, whose own
            run_dir(self.root / "e" / "a" / FLOW),  # another design's, with a target `a` too
        ]
        (self.design / "c").mkdir()  # a target with no run of the flow
        (self.keep[2] / FLOW).mkdir()  # a run directory is never searched
        (self.keep[2] / FLOW / "inner.txt").write_text("a flow's own directory\n")
        (self.keep[3] / FLOW).mkdir()
        (self.keep[3] / FLOW / "inner.txt").write_text("a removed flow's own directory\n")
        self.other_design = [self.keep[4]]

    @property
    def everything(self) -> list[Path]:
        return [*self.direct, *self.a, *self.b, *self.ghost]

    def present(self, paths) -> list[bool]:
        return [p.exists() for p in paths]


def scrub(tmp_path: Path, *args: str, flow: str = FLOW, design: str = "d", json_flag=True):
    result = CliRunner().invoke(
        cli,
        ["scrub", flow, design, "--run-root", str(tmp_path / "xeda_run"), *args]
        + (["--json"] if json_flag else []),
        catch_exceptions=False,
    )
    return result, json.loads(result.stdout) if json_flag else None


# ------------------------------------------------------------------------ the candidate sets


def test_a_target_scrub_removes_that_target_s_runs_of_the_flow_and_nothing_else(
    tmp_path, confirmations
):
    tree = Tree(tmp_path)
    result, document = scrub(tmp_path, "--target", "a")
    assert result.exit_code == 0, result.output
    assert document["success"] is True and document["target"] == "a"
    assert document["scanned"] == [str(tree.design / "a")]
    assert sorted(document["scrubbed"]) == sorted(str(p) for p in tree.a)
    assert document["kept"] == [] and document["gone"] == []
    assert tree.present(tree.a) == [False, False]
    for survivor in (*tree.direct, *tree.b, *tree.ghost, *tree.keep, *tree.other_design):
        assert survivor.exists(), survivor
    assert len(confirmations) == 1


def test_an_unqualified_scrub_removes_the_pre_target_runs_and_every_target_s(
    tmp_path, confirmations
):
    tree = Tree(tmp_path)
    result, document = scrub(tmp_path)
    assert result.exit_code == 0, result.output
    assert document["target"] is None
    # the design's directory, then each directory that is not a run directory, whatever the
    # design file says of it (`ghost`); never `e`, another design's, nor a run directory
    assert document["scanned"] == [
        str(tree.design),
        *(str(tree.design / name) for name in ("a", "b", "c", "ghost")),
    ]
    assert sorted(document["scrubbed"]) == sorted(str(p) for p in tree.everything)
    assert tree.present(tree.everything) == [False] * len(tree.everything)
    for survivor in (*tree.keep, *tree.other_design):
        assert survivor.exists(), survivor
    # one confirmation for the whole collected action, naming every directory it removes
    assert len(confirmations) == 1
    # a target's directory stays, with its durable lock files
    assert (tree.design / "a").is_dir() and (tree.design / "c").is_dir()


def test_a_scrub_that_is_declined_removes_nothing_and_asks_once(tmp_path, confirmations):
    tree = Tree(tmp_path)
    confirmations.answer = "no"
    result, document = scrub(tmp_path)
    assert result.exit_code == 0 and document["success"] is True
    assert document["scrubbed"] == [] and document["kept"] == [] and document["gone"] == []
    assert all(p.exists() for p in (*tree.everything, *tree.keep))
    assert len(confirmations) == 1


def test_the_directories_a_scrub_will_remove_are_listed_before_it_asks(
    tmp_path, confirmations, monkeypatch
):
    tree = Tree(tmp_path)
    shown: list[str] = []
    monkeypatch.setattr(console, "print", lambda *a, **kw: shown.append(" ".join(map(str, a))))
    result, _ = scrub(tmp_path, "--target", "b")
    assert result.exit_code == 0
    listing = "\n".join(shown)
    assert all(str(p) in listing for p in tree.b)
    assert not any(str(p) in listing for p in (*tree.a, *tree.direct, *tree.keep))


def test_a_target_without_a_run_directory_is_nothing_to_do(tmp_path, confirmations):
    tree = Tree(tmp_path)
    for target in ("c", "nothere"):
        result, document = scrub(tmp_path, "--target", target)
        assert result.exit_code == 0, result.output
        assert document["success"] is True and document["scrubbed"] == []
        assert document["target"] == target
    assert confirmations == []
    assert all(p.exists() for p in (*tree.everything, *tree.keep))


def test_nothing_is_scanned_for_a_design_that_has_no_directory(tmp_path, confirmations):
    Tree(tmp_path)
    result, document = scrub(tmp_path, design="nothere")
    assert result.exit_code == 0 and document["scanned"] == [] and document["scrubbed"] == []
    result, document = scrub(tmp_path, "--target", "a", design="nothere")
    assert result.exit_code == 0 and document["scanned"] == [] and document["scrubbed"] == []
    assert confirmations == []


def test_a_removed_flow_is_scrubbed_in_every_target_too(tmp_path, confirmations):
    tree = Tree(tmp_path)
    old = [tree.keep[3], run_dir(tree.design / "a" / "open_xc7")]
    result, document = scrub(tmp_path, flow="open_xc7")
    assert result.exit_code == 0, result.output
    assert sorted(document["scrubbed"]) == sorted(str(p) for p in old)
    assert not any(p.exists() for p in old)
    assert all(p.exists() for p in tree.everything)


def test_a_run_directory_holding_no_documents_is_still_no_target(tmp_path, confirmations):
    """A flow's own run directory that a crashed launch left empty is found by its name's shape
    (it holds the flow's own name below it), never mistaken for a target's directory."""
    tree = Tree(tmp_path)
    empty = tree.design / "yosys_sim"
    (empty / FLOW).mkdir(parents=True)
    (empty / FLOW / "inner.txt").write_text("keep\n")
    # `yosys_sim` is a flow's name: refused as a target name, so never searched
    result, document = scrub(tmp_path)
    assert result.exit_code == 0, result.output
    assert str(empty) not in document["scanned"]
    assert (empty / FLOW / "inner.txt").exists()


# ----------------------------------------------------------------- names, links and ownership


@pytest.mark.parametrize(
    "target",
    [
        "../x",
        "a/b",
        "",
        "has space",
        "1st",
        "..",
        ".",
        FLOW,
        "VivadoSynth",
        "vivado-synth",
        "open_xc7",
        "OpenXC7",
    ],
)
def test_a_target_that_is_no_name_is_refused_before_anything_is_looked_at(
    tmp_path, confirmations, target
):
    tree = Tree(tmp_path)
    outside = tmp_path / "xeda_run" / "x" / FLOW
    run_dir(outside)
    result, document = scrub(tmp_path, "--target", target)
    assert result.exit_code == 1, result.output
    assert document["success"] is False and document["error"]["type"] == "RunDirectoryError"
    assert "target name" in document["error"]["message"] or "name of a flow" in (
        document["error"]["message"]
    )
    assert confirmations == []
    assert all(p.exists() for p in (*tree.everything, *tree.keep, outside))


def test_a_target_directory_that_is_a_link_out_of_the_run_root_is_never_followed(
    tmp_path, confirmations
):
    tree = Tree(tmp_path)
    outside = tmp_path / "outside"
    canary = run_dir(outside / FLOW)
    (tree.design / "out").symlink_to(outside, target_is_directory=True)
    for args in ((), ("--target", "out")):
        result, document = scrub(tmp_path, *args)
        assert result.exit_code == 0, result.output
        assert str(tree.design / "out") not in document["scrubbed"]
        assert canary.exists() and (canary / "out.txt").exists()
        assert (outside / FLOW).is_dir()


def test_a_run_directory_that_is_a_link_out_of_the_run_root_is_never_followed(
    tmp_path, confirmations
):
    tree = Tree(tmp_path)
    outside = tmp_path / "outside"
    canary = run_dir(outside / "elsewhere")
    link = tree.design / "b" / f"{FLOW}_aaaaaaaaaaaaaaaa"
    link.symlink_to(canary, target_is_directory=True)
    result, document = scrub(tmp_path, "--target", "b")
    assert result.exit_code == 0, result.output
    assert canary.exists() and (canary / "out.txt").exists() and link.is_symlink()
    assert sorted(document["scrubbed"]) == sorted(str(p) for p in tree.b)


def test_a_run_directory_that_is_a_link_to_a_directory_beside_it_is_scrubbed_as_that(
    tmp_path, confirmations
):
    """The existing rule: a link that resolves inside its parent in the run root is what it
    leads to, and both are gone."""
    tree = Tree(tmp_path)
    store = run_dir(tree.design / "b" / "store")
    link = tree.design / "b" / f"{FLOW}_bbbbbbbbbbbbbbbb"
    link.symlink_to(store, target_is_directory=True)
    result, document = scrub(tmp_path, "--target", "b")
    assert result.exit_code == 0, result.output
    assert not link.exists() and not link.is_symlink() and not store.exists()
    assert tree.present(tree.b) == [False, False]


def test_two_names_of_one_directory_are_removed_once(tmp_path, confirmations):
    tree = Tree(tmp_path)
    (tree.design / "alias").symlink_to(tree.design / "a", target_is_directory=True)
    result, document = scrub(tmp_path)
    assert result.exit_code == 0, result.output
    assert tree.present(tree.a) == [False, False]
    assert len(document["scrubbed"]) == len(set(document["scrubbed"]))


def test_a_directory_replaced_by_a_link_after_it_was_listed_is_not_removed(
    tmp_path, confirmations, monkeypatch
):
    """Ownership is judged again once the lock is held: the listing is only a candidate set."""
    tree = Tree(tmp_path)
    canary = run_dir(tmp_path / "outside" / "other")
    victim = tree.a[0]
    real_lock = default_runner.run_dir_lock

    def swapping(path, *args, **kwargs):
        if Path(path) == victim and not victim.is_symlink():
            for entry in sorted(victim.iterdir()):
                entry.unlink()
            victim.rmdir()
            victim.symlink_to(canary, target_is_directory=True)
        return real_lock(path, *args, **kwargs)

    monkeypatch.setattr(default_runner, "run_dir_lock", swapping)
    result, document = scrub(tmp_path, "--target", "a")
    assert result.exit_code == 1, result.output
    assert document["success"] is False and document["error"]["type"] == "RunDirectoryError"
    assert canary.exists() and (canary / "out.txt").read_text() == "output\n"
    assert sorted(p.name for p in canary.parent.iterdir()) == ["other"], "no lock file outside"


def test_a_run_that_finished_in_a_candidate_s_place_after_the_listing_is_kept(
    tmp_path, confirmations, monkeypatch, said
):
    """Scrub removes the runs it listed, the ones you confirmed. A run of the same flow that
    finished at the path after the listing is another one: scrub keeps it, says so, and goes on."""
    tree = Tree(tmp_path)
    victim, other = tree.a
    real_lock = default_runner.run_dir_lock

    def swapping(path, *args, **kwargs):
        if Path(path) == victim and not getattr(swapping, "done", False):
            swapping.done = True
            default_runner.RunDirectory.claimed(victim, tree.root).delete()
            run_dir(victim)
            (victim / "out.txt").write_text("a newer run\n")
        return real_lock(path, *args, **kwargs)

    monkeypatch.setattr(default_runner, "run_dir_lock", swapping)
    result, document = scrub(tmp_path, "--target", "a")
    assert result.exit_code == 0, result.output
    assert document["success"] is True and document["scrubbed"] == [str(other)]
    assert document["kept"] == [str(victim)] and document["gone"] == []
    assert (victim / "out.txt").read_text() == "a newer run\n" and not other.exists()
    assert f"kept {victim}: {KEPT}" in said
    assert said[-1] == "1 folders removed, 1 kept."


def test_a_candidate_replaced_by_a_link_to_a_directory_inside_the_root_is_not_removed(
    tmp_path, confirmations, monkeypatch
):
    tree = Tree(tmp_path)
    victim, other = tree.a[0], tree.b[0]
    real_lock = default_runner.run_dir_lock

    def swapping(path, *args, **kwargs):
        if Path(path) == victim and not victim.is_symlink():
            default_runner.RunDirectory.claimed(victim, tree.root).delete()
            victim.symlink_to(other, target_is_directory=True)
        return real_lock(path, *args, **kwargs)

    monkeypatch.setattr(default_runner, "run_dir_lock", swapping)
    result, document = scrub(tmp_path, "--target", "a")
    assert result.exit_code == 1, result.output
    assert document["error"]["type"] == "RunDirectoryError"
    assert (other / "out.txt").exists() and victim.is_symlink()


def test_a_candidate_gone_when_its_lock_is_held_is_skipped_and_not_counted(
    tmp_path, confirmations, monkeypatch, caplog, said
):
    """Another scrub, or a launch that purged it, removed the run directory while this scrub waited
    for its turn. The scrub wanted it gone: it says so, does not count the directory as removed,
    and goes on with the others."""
    tree = Tree(tmp_path)
    victim, other = tree.a
    real_lock = default_runner.run_dir_lock

    def removing(path, *args, **kwargs):
        if Path(path) == victim and victim.exists():
            default_runner.RunDirectory.claimed(victim, tree.root).delete()
        return real_lock(path, *args, **kwargs)

    monkeypatch.setattr(default_runner, "run_dir_lock", removing)
    with caplog.at_level(logging.INFO, logger="xeda.flow_runner.default_runner"):
        result, document = scrub(tmp_path, "--target", "a")
    assert result.exit_code == 0, result.output
    assert document["success"] is True and document["scrubbed"] == [str(other)]
    assert document["gone"] == [str(victim)] and document["kept"] == []
    assert tree.present(tree.a) == [False, False]
    # By the message itself: the command line's log filter shortens a record's logger name in
    # place, so a name test depends on which tests ran first in this process.
    skipped = [
        record.getMessage()
        for record in caplog.records
        if record.levelno == logging.INFO and record.getMessage().startswith("Not removing ")
    ]
    assert skipped == [f"Not removing {victim}: it is gone already"], caplog.text
    assert f"{victim} is gone already" in said
    assert said[-1] == "1 folders removed, 1 gone already."


def launch_writes_into(directory: Path) -> None:
    """What a launch does in its own run directory while it holds the lock: files come and go in
    it, so its inode change time moves. Repeated until it has moved, since a file system with a
    coarse clock may show no change after the first file."""
    before = directory.stat().st_ctime_ns
    for attempt in range(500):
        (directory / f"launch-{attempt}.txt").write_text("a file the launch wrote\n")
        if directory.stat().st_ctime_ns != before:
            return
        time.sleep(0.01)
    raise AssertionError(f"the inode change time of {directory} did not move")


def test_a_launch_writing_into_a_candidate_while_scrub_waits_for_its_lock_does_not_stop_the_scrub(
    tmp_path, monkeypatch
):
    """What the `--scrub` of one launch meets: it lists the run directory of another variant, which
    is being launched, and waits for its lock. A launch that finds its run fresh writes a marker in
    the directory to read the file-system clock. That changes no run record. The directory is the
    one that was listed with more files in it, and still a run directory of the flow: it is
    removed, and the writes do not fail the scrub. (A launch may also refresh the trace once its
    records have settled. That changes a record, and the directory is kept: see the next tests.)"""
    tree = Tree(tmp_path)
    held = tree.a[0]
    listed = threading.Event()
    outcome: dict = {}

    def ask(prompt="", *args, **kwargs):
        listed.set()  # the candidates are listed: scrub asks, then waits for each lock
        return "yes"

    def scrubbing():
        try:
            outcome["result"] = default_runner.scrub_design(
                FLOW, tree.design, run_root=tree.root, target="a"
            )
        except BaseException as error:  # noqa: BLE001 - reported by the test thread, below
            outcome["error"] = error

    monkeypatch.setattr(console, "input", ask)
    with run_dir_lock(held, tree.root):
        thread = threading.Thread(target=scrubbing)
        thread.start()
        assert listed.wait(timeout=30), "scrub did not list the run directories"
        launch_writes_into(held)
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert "error" not in outcome, repr(outcome.get("error"))
    assert sorted(outcome["result"].removed) == sorted(tree.a)
    assert tree.present(tree.a) == [False, False]


def a_run_ends_in(directory: Path) -> None:
    """What a launch writes when its run ends: `results.json` and `trace.json`, each made whole
    and then renamed over the one before, which is what `replacing_file` does."""
    for name in ("results.json", "trace.json"):
        with replacing_file(directory / name) as document:
            document.write('{"success": true}\n')


@pytest.mark.parametrize("before", ["a run was there", "no run was there"])
def test_a_run_that_ends_in_a_candidate_while_scrub_waits_for_its_lock_is_kept(
    tmp_path, monkeypatch, said, before
):
    """The run directory of another variant is being launched when scrub lists it. The launch ends
    its run before scrub gets the lock, so the directory holds a run that was not there to be
    listed, or another run than the one that was. It is kept, whether the listing saw a run's
    documents or none."""
    tree = Tree(tmp_path)
    held, other = tree.a
    if before == "no run was there":
        for name in ("results.json", "trace.json"):
            (held / name).unlink(missing_ok=True)
    listed = threading.Event()
    outcome: dict = {}

    def ask(prompt="", *args, **kwargs):
        listed.set()
        return "yes"

    def scrubbing():
        try:
            outcome["result"] = default_runner.scrub_design(
                FLOW, tree.design, run_root=tree.root, target="a"
            )
        except BaseException as error:  # noqa: BLE001 - reported by the test thread, below
            outcome["error"] = error

    monkeypatch.setattr(console, "input", ask)
    with run_dir_lock(held, tree.root):
        thread = threading.Thread(target=scrubbing)
        thread.start()
        assert listed.wait(timeout=30), "scrub did not list the run directories"
        a_run_ends_in(held)
    thread.join(timeout=30)
    assert not thread.is_alive()
    assert "error" not in outcome, repr(outcome.get("error"))
    assert outcome["result"].removed == [other] and not other.exists()
    assert (held / "results.json").read_text() == '{"success": true}\n'
    assert f"kept {held}: {KEPT}" in said


def test_only_a_missing_name_is_gone(tmp_path):
    """Gone means nothing is there. A file is something, and so is a link, wherever it leads. A
    path that cannot be told -- a file where a directory should be, a directory that cannot be
    searched -- is not gone either: scrub judges it, and refuses it."""
    is_gone = default_runner._is_gone
    (tmp_path / "file").write_text("x\n")
    (tmp_path / "dir").mkdir()
    (tmp_path / "dangling").symlink_to(tmp_path / "nowhere", target_is_directory=True)
    assert is_gone(tmp_path / "missing")
    assert is_gone(tmp_path / "dir" / "missing")
    assert is_gone(tmp_path / "no" / "such" / "directory")
    assert not is_gone(tmp_path / "file")
    assert not is_gone(tmp_path / "dir")
    assert not is_gone(tmp_path / "dangling")
    assert not is_gone(tmp_path / "file" / "below")
    if os.geteuid() != 0:
        locked = tmp_path / "locked"
        locked.mkdir()
        (locked / "inside").write_text("x\n")
        locked.chmod(0)
        try:
            assert not is_gone(locked / "inside")
        finally:
            locked.chmod(0o755)


def test_two_scrubs_that_listed_the_same_run_directories_both_succeed(tmp_path, monkeypatch):
    """`xeda scrub` twice at once, or a launch with `--scrub` beside another scrub, list the same
    run directories. Each one goes to the scrub that locks it first. The other finds it gone,
    which is what it wanted, and goes on: neither scrub fails, and each directory is removed once.
    """
    tree = Tree(tmp_path)
    both_listed = threading.Barrier(2)
    outcomes: list = []

    def ask(prompt="", *args, **kwargs):
        both_listed.wait(timeout=30)  # neither scrub removes a directory before both have listed
        return "yes"

    def scrubbing():
        try:
            outcomes.append(
                default_runner.scrub_design(FLOW, tree.design, run_root=tree.root, target="a")
            )
        except BaseException as error:  # noqa: BLE001 - reported by the test thread, below
            outcomes.append(error)

    monkeypatch.setattr(console, "input", ask)
    threads = [threading.Thread(target=scrubbing) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    assert all(isinstance(outcome, default_runner.ScrubResult) for outcome in outcomes), outcomes
    assert sorted(p for outcome in outcomes for p in outcome.removed) == sorted(tree.a)
    assert tree.present(tree.a) == [False, False]


def test_two_scrubs_that_listed_the_same_link_candidate_both_succeed(tmp_path, monkeypatch):
    """The two-scrub case with a candidate that is a link to a directory beside it. A link is
    locked by the directory it leads to. That stays one lock when the scrub holding it has removed
    the directory and then the link, so the other, which waited, finds the link gone and goes on
    instead of failing to lock a link that is no more."""
    tree = Tree(tmp_path)
    store = run_dir(tree.design / "x" / "store")
    link = tree.design / "x" / f"{FLOW}_bbbbbbbbbbbbbbbb"
    link.symlink_to(store, target_is_directory=True)
    both_listed = threading.Barrier(2)
    both_asking = threading.Barrier(2)
    real_flock = run_lock.fcntl.flock
    outcomes: list = []

    def ask(prompt="", *args, **kwargs):
        both_listed.wait(timeout=30)
        return "yes"

    def flock_once_both_are_asking(descriptor, mode):
        if mode == run_lock.fcntl.LOCK_EX:
            both_asking.wait(timeout=30)  # each has named the lock it asks for, and none holds it
        return real_flock(descriptor, mode)

    def scrubbing():
        try:
            outcomes.append(
                default_runner.scrub_design(FLOW, tree.design, run_root=tree.root, target="x")
            )
        except BaseException as error:  # noqa: BLE001 - reported by the test thread, below
            outcomes.append(error)

    monkeypatch.setattr(console, "input", ask)
    monkeypatch.setattr(run_lock.fcntl, "flock", flock_once_both_are_asking)
    threads = [threading.Thread(target=scrubbing) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60)
    assert not any(thread.is_alive() for thread in threads)
    assert all(isinstance(outcome, default_runner.ScrubResult) for outcome in outcomes), outcomes
    assert sorted(p for outcome in outcomes for p in outcome.removed) == [link]
    assert not os.path.lexists(link) and not store.exists()


def test_a_link_that_leads_below_its_directory_is_left_alone(tmp_path, confirmations):
    """A run directory is a child of the directory it is listed in, or a link to a directory beside
    it. A link named like one that leads below it, into a target's directory, is neither: scrub
    leaves it, and what it leads to is a candidate in its own directory."""
    tree = Tree(tmp_path)
    link = tree.design / f"{FLOW}_cccccccccccccccc"
    link.symlink_to(tree.a[0], target_is_directory=True)
    result, document = scrub(tmp_path)
    assert result.exit_code == 0, result.output
    assert str(link) not in document["scrubbed"] and str(tree.a[0]) in document["scrubbed"]
    assert link.is_symlink() and not link.exists(), "left as it was, leading to what is now gone"
    assert tree.present(tree.everything) == [False] * len(tree.everything)


def test_a_design_directory_that_is_a_link_out_of_the_run_root_is_never_searched(
    tmp_path, confirmations
):
    tree = Tree(tmp_path)
    canary = run_dir(tmp_path / "outside" / FLOW)
    (tree.root / "linked").symlink_to(canary.parent, target_is_directory=True)
    result, document = scrub(tmp_path, design="linked")
    assert result.exit_code == 0, result.output
    assert document["scrubbed"] == [] and confirmations == []
    assert canary.exists() and (canary / "out.txt").exists()


def vacate(path: Path, tree: Tree) -> None:
    default_runner.RunDirectory.claimed(path, tree.root).delete()


def files_added(path: Path, tree: Tree, outside: Path) -> None:
    """A launch that finds its run fresh writes a marker in the directory to read the file-system
    clock, and changes no run record."""
    (path / "clock-marker").write_text("x\n")


def results_made_again(path: Path, tree: Tree, outside: Path) -> None:
    """A run ends in the directory, which held a `results.json` of the run before."""
    with replacing_file(path / "results.json") as document:
        document.write('{"success": true}\n')


def trace_refreshed(path: Path, tree: Tree, outside: Path) -> None:
    """A launch that finds its run fresh refreshes the trace once its records have settled: it is
    made whole and renamed over the one before, which changes no result."""
    with replacing_file(path / "trace.json") as document:
        document.write('{"refreshed": true}\n')


def newer_run(path: Path, tree: Tree, outside: Path) -> None:
    vacate(path, tree)
    run_dir(path)
    (path / "out.txt").write_text("a newer run\n")


def link_to_a_directory_beside_it(path: Path, tree: Tree, outside: Path) -> None:
    vacate(path, tree)
    path.symlink_to(run_dir(path.parent / "store"), target_is_directory=True)


def link_out_of_the_run_root(path: Path, tree: Tree, outside: Path) -> None:
    vacate(path, tree)
    path.symlink_to(run_dir(outside / "other"), target_is_directory=True)


def link_to_another_targets_run_directory(path: Path, tree: Tree, outside: Path) -> None:
    vacate(path, tree)
    path.symlink_to(tree.b[0], target_is_directory=True)


def a_file(path: Path, tree: Tree, outside: Path) -> None:
    vacate(path, tree)
    path.write_text("no directory\n")


def link_below_its_directory(path: Path, tree: Tree, outside: Path) -> None:
    vacate(path, tree)
    path.symlink_to(run_dir(path.parent / "inner" / "run"), target_is_directory=True)


def link_to_nowhere(path: Path, tree: Tree, outside: Path) -> None:
    vacate(path, tree)
    path.symlink_to(tree.root / "gone", target_is_directory=True)


def renamed_away(path: Path, tree: Tree, outside: Path) -> None:
    """Nothing is at the path any more."""
    vacate(path, tree)


def entries(base: Path, *, locks: bool) -> list[tuple[str, str]]:
    """Every entry under `base`, with what it is: a directory, a link (and where it leads) or a file
    (and its size). A lock file is one entry too, if `locks` says so."""
    found = []
    for directory, names, files in os.walk(base, followlinks=False):
        for name in (*names, *files):
            entry = Path(directory) / name
            if name.endswith(".lock") and not locks:
                continue
            if entry.is_symlink():
                what = f"link to {os.readlink(entry)}"
            elif entry.is_dir():
                what = "directory"
            else:
                what = f"file of {entry.stat().st_size} bytes"
            found.append((str(entry.relative_to(base)), what))
    return sorted(found)


@pytest.mark.parametrize(
    ("replacement", "outcome"),
    [
        (files_added, "removed"),
        (results_made_again, "kept"),
        (trace_refreshed, "kept"),
        (newer_run, "kept"),
        (link_to_a_directory_beside_it, "kept"),
        (link_below_its_directory, "refused"),
        (link_out_of_the_run_root, "refused"),
        (link_to_another_targets_run_directory, "refused"),
        (link_to_nowhere, "refused"),
        (a_file, "refused"),
        (renamed_away, "gone"),
    ],
    ids=lambda value: value if isinstance(value, str) else value.__name__,
)
def test_what_scrub_does_with_a_candidate_that_changed_after_it_was_listed(
    tmp_path, confirmations, monkeypatch, replacement, outcome
):
    """One rule for what scrub does with a directory, judged twice: when it lists the directory and
    again when it holds the directory's lock. Whatever is at the first candidate's path -- put
    there before the listing, or only after it -- is what scrub would have listed or it is not. If
    it would have been listed, it is removed, unless its run records changed after the listing:
    then scrub keeps it. If nothing is there, scrub skips it and goes on. If something else is
    there, scrub refuses it with an error and removes nothing more."""

    def tree_with_the_first_candidate_replaced(base: Path, *, at_once: bool):
        base.mkdir()
        outside = base / "outside"
        outside.mkdir()
        tree = Tree(base)
        victim = tree.a[0]

        def replace() -> None:
            replacement(victim, tree, outside)

        if at_once:
            replace()
        return tree, victim, replace

    tree, victim, _ = tree_with_the_first_candidate_replaced(tmp_path / "listed", at_once=True)
    result = default_runner.scrub_design(FLOW, tree.design, run_root=tree.root, target="a")
    listed = outcome in ("removed", "kept")
    assert (victim in result.removed) == listed, "it is listed exactly when it can be removed"

    later = tmp_path / "later"
    tree, victim, replace = tree_with_the_first_candidate_replaced(later, at_once=False)
    second = tree.a[1]
    real_lock = default_runner.run_dir_lock
    after_the_swap: list[list[tuple[str, str]]] = []

    def swapping(path, *args, **kwargs):
        if Path(path) == victim and not after_the_swap:
            replace()
            after_the_swap.append(entries(later, locks=False))
        return real_lock(path, *args, **kwargs)

    monkeypatch.setattr(default_runner, "run_dir_lock", swapping)
    refusal = None
    try:
        result = default_runner.scrub_design(FLOW, tree.design, run_root=tree.root, target="a")
    except RunDirectoryError as error:
        refusal = error
    if outcome == "removed":
        assert refusal is None, refusal
        assert victim in result.removed and not os.path.lexists(victim)
    elif outcome == "kept":
        assert refusal is None, refusal
        assert victim not in result.removed and os.path.lexists(victim)
        assert result.removed == [second] and not os.path.lexists(second)
    elif outcome == "gone":
        assert refusal is None, refusal
        assert result.removed == [second] and not os.path.lexists(second)
    else:
        assert refusal is not None
        assert entries(later, locks=False) == after_the_swap[0], "something was removed"


@pytest.mark.parametrize("leads_to", ["outside", "nowhere"])
def test_a_candidate_that_became_a_link_out_of_the_run_root_or_to_nowhere_makes_nothing(
    tmp_path, monkeypatch, leads_to
):
    """The lock refuses such a link before it creates anything, a lock file included. A scrub that
    locked the directory a link leads to would make a lock file where the link points, and the
    directories above it."""
    tree = Tree(tmp_path)
    victim = tree.a[0]
    target = run_dir(tmp_path / "outside" / "other") if leads_to == "outside" else None
    after_the_swap: list[list[tuple[str, str]]] = []

    def ask(prompt="", *args, **kwargs):  # after the listing: the candidate becomes the link
        default_runner.RunDirectory.claimed(victim, tree.root).delete()
        victim.symlink_to(
            target if target is not None else tree.root / "far" / "away",
            target_is_directory=True,
        )
        after_the_swap.append(entries(tmp_path, locks=True))
        return "yes"

    monkeypatch.setattr(console, "input", ask)
    with pytest.raises(RunDirectoryError):
        default_runner.scrub_design(FLOW, tree.design, run_root=tree.root, target="a")
    assert entries(tmp_path, locks=True) == after_the_swap[0]


def test_a_link_to_a_directory_in_the_run_root_and_the_directory_share_one_lock(tmp_path):
    """`get_flow_run_path` uses a run directory that is a link resolving inside the run root:
    a launch through the link's name and one through the real name serialize."""
    from xeda.flow_runner.run_lock import lock_file, run_dir_lock

    tree = Tree(tmp_path)
    real = tree.a[0]
    alias = tree.design / "b" / f"{FLOW}_cccccccccccccccc"
    alias.symlink_to(real, target_is_directory=True)
    assert lock_file(alias, tree.root) == lock_file(real, tree.root)
    assert lock_file(alias, tree.root) == tree.design.resolve() / "a" / f"{real.name}.lock"
    with run_dir_lock(real, tree.root):
        probe = subprocess.run(
            [
                sys.executable,
                "-c",
                "import fcntl, sys;"
                "f = open(sys.argv[1], 'a');"
                "fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)",
                str(lock_file(alias, tree.root)),
            ],
            capture_output=True,
        )
        assert probe.returncode != 0, "the alias's lock was free while the real name's was held"
    assert not (tree.design / "b" / f"{alias.name}.lock").exists()


def test_without_a_run_root_a_lock_is_beside_the_name_it_is_given(tmp_path):
    from xeda.flow_runner.run_lock import lock_file

    tree = Tree(tmp_path)
    outside = tmp_path / "outside"
    run_dir(outside / "other")
    link = tree.design / "b" / f"{FLOW}_dddddddddddddddd"
    link.symlink_to(outside / "other", target_is_directory=True)
    assert lock_file(link) == tree.design.resolve() / "b" / f"{link.name}.lock"


@pytest.mark.parametrize("leads_to", ["outside", "nowhere"])
def test_a_link_out_of_the_run_root_or_nowhere_is_not_locked(tmp_path, leads_to):
    from xeda.flow_runner.run_lock import run_dir_lock, run_dir_read_lock

    tree = Tree(tmp_path)
    outside = tmp_path / "outside"
    run_dir(outside / "other")
    before = sorted(p.name for p in outside.iterdir())
    link = tree.design / "b" / f"{FLOW}_cccccccccccccccc"
    link.symlink_to(
        outside / "other" if leads_to == "outside" else tree.root / "gone",
        target_is_directory=True,
    )
    listing = sorted(p.name for p in link.parent.iterdir())
    for lock in (run_dir_lock, run_dir_read_lock):
        with pytest.raises(RunDirectoryError, match="not locked"):
            with lock(link, run_root=tree.root):
                pytest.fail("locked")
    assert sorted(p.name for p in outside.iterdir()) == before
    assert sorted(p.name for p in link.parent.iterdir()) == listing, "nothing created"


def test_a_run_directory_reached_through_a_link_out_of_the_run_root_is_not_locked(tmp_path):
    from xeda.flow_runner.run_lock import run_dir_lock, run_dir_read_lock

    tree = Tree(tmp_path)
    outside = tmp_path / "outside"
    outside.mkdir()
    (tree.design / "out").symlink_to(outside, target_is_directory=True)
    for lock in (run_dir_lock, run_dir_read_lock):
        with pytest.raises(RunDirectoryError, match="run root"):
            with lock(tree.design / "out" / FLOW, run_root=tree.root):
                pytest.fail("locked")
    assert list(outside.iterdir()) == []


def test_scrub_waits_for_a_reader_and_judges_the_directory_again_afterwards(
    tmp_path, confirmations
):
    """A producer held by a consumer's read lease is not removed under it."""
    tree = Tree(tmp_path)
    held = tree.a[0]
    outcome: dict = {}

    def scrubbing():
        outcome["result"] = default_runner.scrub_design(
            FLOW, tree.design, run_root=tree.root, target="a"
        )

    with run_dir_read_lock(held):
        thread = threading.Thread(target=scrubbing)
        thread.start()
        time.sleep(0.5)
        assert thread.is_alive(), "the scrub did not wait for the reader"
        assert held.exists() and (held / "out.txt").exists()
    thread.join(timeout=30)
    assert not thread.is_alive() and not held.exists()
    assert sorted(outcome["result"].removed) == sorted(tree.a)
    assert os.path.exists(held.parent / f"{held.name}.lock"), "the durable lock file stays"


# ----------------------------------------------------------------------- the API, and --scrub


def test_scrub_design_reports_what_it_scanned_and_removed(tmp_path, confirmations):
    tree = Tree(tmp_path)
    result = default_runner.scrub_design(FLOW, tree.design, run_root=tree.root, target="b")
    assert result.scanned == [tree.design / "b"]
    assert sorted(result.removed) == sorted(tree.b)
    assert result.kept == [] and result.gone == []
    everything = default_runner.scrub_design(FLOW, tree.design, run_root=tree.root)
    assert tree.design in everything.scanned
    assert sorted(everything.removed) == sorted([*tree.direct, *tree.a, *tree.ghost])


def maker_design(tmp_path: Path, target: str) -> Design:
    design = tmp_path / "design"
    design.mkdir(exist_ok=True)
    (design / "d.yaml").write_text(
        'name: d\nrtl: {sources: [], top: "t"}\ntargets: {a: {}, b: {}}\n'
    )
    return Design.from_file(design / "d.yaml", target=target)


def test_the_scrub_of_a_launch_stays_in_its_own_target(tmp_path, confirmations):
    def launch(target: str, text: str, scrub_old_runs: bool):
        launcher = DefaultRunner(
            tmp_path / "xeda_run",
            hashed_run_dirs=True,
            scrub_old_runs=scrub_old_runs,
            display_results=False,
        )
        flow = launcher.launch_flow(_Maker, maker_design(tmp_path, target), {"text": text})
        assert flow.succeeded
        return flow.run_path

    a_old, b_old = launch("a", "one\n", False), launch("b", "one\n", False)
    assert a_old.parent.name == "a" and b_old.parent.name == "b"
    a_new = launch("a", "two\n", True)
    assert a_new.parent.name == "a" and a_new != a_old
    assert not a_old.exists(), "the target's own earlier variant is scrubbed"
    assert b_old.exists(), "another target's run is not"
    assert len(confirmations) == 1


def test_scrub_runs_keeps_its_one_directory_contract(tmp_path, confirmations):
    """`--scrub` and the embedded oracles call `scrub_runs(flow, directory, exclude, run_root)`."""
    tree = Tree(tmp_path)
    assert (
        default_runner.scrub_runs(FLOW, tree.design, [tree.direct[0]], run_root=tree.root) is True
    )
    assert tree.present(tree.direct) == [True, False]
    assert all(p.exists() for p in (*tree.a, *tree.b, *tree.ghost)), "no directory below it"


def test_a_launch_never_lists_its_own_run_directory_when_a_sibling_removes_it_meanwhile(
    tmp_path, monkeypatch
):
    """Two launches of variants of one flow, each with `--scrub`, remove each other's run
    directory. A launch's own can go while it lists: seen as a directory, then gone. It is still
    its own, and no candidate: the listing leaves it out whether or not it exists."""
    tree = Tree(tmp_path)
    own, sibling = tree.direct
    real_judgment = default_runner._is_run_directory

    def seen_then_removed(path, *args, **kwargs):
        found = real_judgment(path, *args, **kwargs)
        if path == own and found:
            default_runner.RunDirectory.claimed(own, tree.root).delete()  # the sibling's scrub
        return found

    monkeypatch.setattr(default_runner, "_is_run_directory", seen_then_removed)
    listed = default_runner._run_directories_in(FLOW, tree.design, [own], tree.root)
    assert listed == [sibling]


def test_a_directory_is_excluded_by_any_path_that_resolves_to_it(tmp_path, confirmations):
    """`exclude` names a directory by where it resolves to, a link to it included."""
    tree = Tree(tmp_path)
    alias = tree.design / "alias"
    alias.symlink_to(tree.direct[0], target_is_directory=True)
    assert default_runner.scrub_runs(FLOW, tree.design, [alias], run_root=tree.root) is True
    assert tree.present(tree.direct) == [True, False]


# ------------------------------------------------------------------------- the JSON document


def three_outcomes(tree: Tree) -> tuple[Path, Path, Path]:
    """In target `a`: a run directory that is gone by its turn, one that a run ends in, and one that
    nothing happens to."""
    gone, kept = tree.a
    return gone, kept, run_dir(tree.design / "a" / OTHER_HASHED)


def test_scrub_json_lists_what_it_kept_and_found_gone_and_says_so_on_stderr(
    tmp_path, confirmations, monkeypatch
):
    """`--json` keeps stdout for one document. It lists the directories scrub removed, kept and
    found gone, by key. What scrub says of them goes to stderr, with the rest of its output."""
    tree = Tree(tmp_path)
    gone, kept, removed = three_outcomes(tree)
    real_lock = default_runner.run_dir_lock

    def meanwhile(path, *args, **kwargs):
        if Path(path) == gone and gone.exists():
            default_runner.RunDirectory.claimed(gone, tree.root).delete()
        if Path(path) == kept:
            a_run_ends_in(kept)
        return real_lock(path, *args, **kwargs)

    monkeypatch.setattr(default_runner, "run_dir_lock", meanwhile)
    result, document = scrub(tmp_path, "--target", "a")
    assert result.exit_code == 0, result.output
    assert result.stdout.lstrip().startswith("{") and result.stdout.rstrip().endswith("}")
    assert document["success"] is True
    assert document["scrubbed"] == [str(removed)]
    assert document["kept"] == [str(kept)]
    assert document["gone"] == [str(gone)]
    assert f"kept {kept}: {KEPT}" in result.stderr
    assert f"{gone} is gone already" in result.stderr
    assert "1 folders removed, 1 kept, 1 gone already." in result.stderr


def test_xeda_scrub_json_writes_one_document_to_stdout_when_it_kept_and_found_gone(tmp_path):
    """The command as a script runs it. Scrub lists, and waits for the answer. While it waits, one
    candidate is removed and a run ends in another. Stdout is then one JSON document, and what
    scrub said of the outcomes is on stderr."""
    tree = Tree(tmp_path)
    gone, kept, removed = three_outcomes(tree)
    process = subprocess.Popen(
        [sys.executable, "-m", "xeda", "scrub", FLOW, "d", "--target", "a", "--json"]
        + ["--run-root", str(tree.root)],
        cwd=tmp_path,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    errors: queue.Queue = queue.Queue()
    reader = threading.Thread(target=lambda: [errors.put(line) for line in process.stderr])
    reader.daemon = True
    reader.start()
    try:
        said = [errors.get(timeout=120)]
        while "This will remove all of the following" not in said[-1]:
            said.append(errors.get(timeout=120))
        # the candidates are listed, with what the listing saw of their run records: change them
        default_runner.RunDirectory.claimed(gone, tree.root).delete()
        a_run_ends_in(kept)
        assert process.stdin is not None and process.stdout is not None
        process.stdin.write("yes\n")
        process.stdin.flush()
        stdout = process.stdout.read()
        process.wait(timeout=120)
        reader.join(timeout=30)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=30)
    while not errors.empty():
        said.append(errors.get())
    assert process.returncode == 0, "".join(said)
    document = json.loads(stdout)
    assert document["success"] is True
    assert document["scrubbed"] == [str(removed)]
    assert document["kept"] == [str(kept)]
    assert document["gone"] == [str(gone)]
    errors_text = "".join(said)
    assert f"kept {kept}: {KEPT}" in errors_text and f"{gone} is gone already" in errors_text
    assert "1 folders removed, 1 kept, 1 gone already." in errors_text


# ------------------------------------------------------------------------------- the help text


def test_scrub_help_describes_both_readings():
    result = CliRunner().invoke(cli, ["scrub", "--help"])
    assert result.exit_code == 0
    import click

    text = " ".join(click.unstyle(result.output).split())
    assert "--target" in text and "every target" in text and "<design_name>" in text


def test_the_scrub_of_an_unmarked_run_root_is_refused(tmp_path):
    (tmp_path / "xeda_run" / "d").mkdir(parents=True)
    (tmp_path / "xeda_run" / "d" / "keep").write_text("mine\n")
    result, document = scrub(tmp_path, "--target", "a")
    assert result.exit_code == 1 and document["success"] is False
    assert (tmp_path / "xeda_run" / "d" / "keep").exists()


def test_run_directory_error_is_what_a_refused_name_raises(tmp_path):
    tree = Tree(tmp_path)
    with pytest.raises(RunDirectoryError, match="target name"):
        default_runner.scrub_design(FLOW, tree.design, run_root=tree.root, target="../x")
