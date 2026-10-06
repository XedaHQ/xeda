"""`xeda scrub` beside per-target run directories (PT-3, PTD3-4).

A run directory lies in `<run root>/<design>[/<target>]/<flow>[_<hash>]`, so a scrub of one flow
of one design has three honest readings: everything (`xeda scrub FLOW DESIGN`: the pre-target
`<design>/<flow>` runs and every target's), or one target's alone (`--target NAME`), and it never
reads a design file: it finds what is on disk, one level below the design for targets, and
never goes into a run directory. The oracles (the plan's O-SCRUB, and O-LEGACY's scrub half):

- exact candidate sets, collected before one confirmation, removed under each directory's lock;
- a target's scrub leaves the pre-target runs and every other target alone, and an unqualified
  one takes them all, a target no design file names included;
- a refused name touches nothing, and so does a link that leads out of the run root;
- a held read lease blocks the scrub until released, and ownership is judged again after it;
- the `--scrub` of a launch cannot reach another target.
"""

import json
import os
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
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner import default_runner
from xeda.flow_runner.run_lock import run_dir_read_lock
from xeda.run_dir import RunDirectoryError
from xeda.run_root import ensure_run_root

from .io_flows import _Maker

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX run-directory locks")

FLOW = "vivado_synth"
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


def run_dir(path: Path) -> Path:
    """A run directory as a launch leaves one: its documents, and a file of its own."""
    path.mkdir(parents=True)
    (path / "settings.json").write_text("{}")
    (path / "results.json").write_text("{}")
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
    assert document["scrubbed"] == []
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


def test_a_candidate_replaced_by_another_directory_is_not_removed(
    tmp_path, confirmations, monkeypatch
):
    """Under the lock the candidate is judged again, down to its identity: a different
    directory of the same name (another run's, made meanwhile) is not what was listed."""
    tree = Tree(tmp_path)
    victim = tree.a[0]
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
    assert result.exit_code == 1, result.output
    assert document["error"]["type"] == "RunDirectoryError"
    assert (victim / "out.txt").read_text() == "a newer run\n"


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
