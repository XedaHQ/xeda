"""What a run directory is, for a launch and for `xeda scrub`: one definition
(`run_dir.run_directory_problem`).

A run directory of a flow is named `<flow>` or `<flow>_<hash>` and is a directory in the design's
(or the target's) directory. A link counts only when it leads, where its chain ends, to a
directory of that kind beside it. A link to a target's directory, to another flow's run directory,
to a directory below or above, out of the run root, to a file or to nowhere is neither run in nor
scrubbed, and scrub says so. The oracles:

- a table of link cases, judged three ways that must agree: scrub lists a link if and only if a
  launch accepts it (`run_path_of`), and every other link is reported and left alone;
- scrub leaves what a refused link leads to as it was, and does not wait for the lock of the run
  that lives there;
- a launch refuses such a link before it writes anything;
- a link retargeted while scrub waits for a lock is refused, and nothing is removed.
"""

import contextlib
import json
import os
import sys
import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from xeda import Design
from xeda.cli import cli
from xeda.console import console
from xeda.flow import registered_flows
from xeda.flow_runner import DefaultRunner, default_runner
from xeda.flow_runner.dse.dse_runner import _purge_run
from xeda.flow_runner.run_lock import run_dir_lock
from xeda.run_dir import (
    DIR_NAME_HASH_LEN,
    RunDirectory,
    RunDirectoryError,
    run_directory_name,
    run_directory_problem,
)
from xeda.run_root import ensure_run_root

from .io_flows import _Maker

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX run-directory locks")

FLOW = "vivado_synth"
HASH = "0123456789abcdef"
OTHER_HASH = "fedcba9876543210"
A_THIRD_HASH = "aaaaaaaaaaaaaaaa"
RULE = f"named {FLOW} or {FLOW}_<hash>"


@pytest.fixture(autouse=True)
def private_registry():
    before = registered_flows.copy()
    yield
    registered_flows.clear()
    registered_flows.update(before)


@pytest.fixture
def confirmations(monkeypatch):
    prompts: list[str] = []

    def ask(prompt="", *args, **kwargs):
        prompts.append(str(prompt))
        return "yes"

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
    """A run directory as a launch leaves one after a success."""
    path.mkdir(parents=True)
    for name in ("settings.json", "results.json", "trace.json"):
        (path / name).write_text("{}")
    (path / "out.txt").write_text("output\n")
    return path


def entries(base: Path) -> list[tuple[str, str]]:
    """Every entry under `base` with what it is, links as links: a lock file counts."""
    found = []
    for directory, names, files in os.walk(base, followlinks=False):
        for name in (*names, *files):
            entry = Path(directory) / name
            if entry.is_symlink():
                what = f"link to {os.readlink(entry)}"
            elif entry.is_dir():
                what = "directory"
            else:
                what = f"file of {entry.stat().st_size} bytes"
            found.append((str(entry.relative_to(base)), what))
    return sorted(found)


class World:
    """`<run root>/d/a` is the directory a link is made in (target `a` of design `d`), beside
    the run directories and directories a link must not stand for."""

    def __init__(self, tmp_path: Path) -> None:
        root = ensure_run_root(tmp_path / "xeda_run")
        assert root is not None
        self.root = root
        self.design = root / "d"
        self.parent = self.design / "a"
        self.other_target = self.design / "b"
        self.outside = tmp_path / "outside"
        #: what no link may lead scrub or a launch to: every one keeps its files
        self.victims = {
            "another flow's run directory": run_dir(self.parent / "yosys_fpga"),
            "a directory with another name": run_dir(self.parent / "store"),
            "a name that is no hash": run_dir(self.parent / f"{FLOW}_other"),
            "below": run_dir(self.parent / "sub" / FLOW),
            "another target's run directory": run_dir(self.other_target / FLOW),
            "another target's other flow": run_dir(self.other_target / "yosys_fpga"),
            "the design's other flow": run_dir(self.design / "yosys_fpga"),
            "the design's own run directory": run_dir(self.design / FLOW),
            "outside the run root": run_dir(self.outside / FLOW),
        }
        #: a run directory of the flow beside the links: scrub lists it, whatever the links are
        self.sibling = run_dir(self.parent / f"{FLOW}_{OTHER_HASH}")
        (self.parent / "file.txt").write_text("a file\n")

    def link(self, hashed: bool) -> Path:
        return self.parent / (f"{FLOW}_{HASH}" if hashed else FLOW)

    def launcher(self, hashed: bool) -> DefaultRunner:
        return DefaultRunner(self.root, display_results=False, hashed_run_dirs=hashed)

    def accepted_by_a_launch(self, hashed: bool) -> bool:
        try:
            self.launcher(hashed).run_path_of(
                "d", FLOW, HASH + "ffff" if hashed else None, target="a"
            )
        except RunDirectoryError:
            return False
        return True

    def intact(self) -> None:
        for what, path in self.victims.items():
            assert (path / "out.txt").read_text() == "output\n", what
            assert (path / "results.json").exists(), what
        assert (self.parent / "file.txt").read_text() == "a file\n"


# Each case makes what stands at the link's name: a link (to what `target` gives, or through
# links), or a real directory.
def to(target):
    def make(world: World, link: Path) -> None:
        link.symlink_to(target(world), target_is_directory=True)

    return make


def to_a_hop(world: World, link: Path) -> None:
    hop = world.parent / "hop"
    hop.symlink_to(world.sibling, target_is_directory=True)
    link.symlink_to(hop, target_is_directory=True)


def to_a_link_that_leads_elsewhere(world: World, link: Path) -> None:
    middle = world.parent / f"{FLOW}_{A_THIRD_HASH}"
    middle.symlink_to(world.other_target, target_is_directory=True)
    link.symlink_to(middle, target_is_directory=True)


def to_itself(world: World, link: Path) -> None:
    link.symlink_to(link.name)


def a_real_directory(world: World, link: Path) -> None:
    run_dir(link)


#: name -> (what makes the link, whether a launch and scrub both accept it)
CASES = {
    "a target's directory": (to(lambda w: w.other_target), False),
    "a run directory of another target": (
        to(lambda w: w.victims["another target's run directory"]),
        False,
    ),
    "another flow's run directory": (
        to(lambda w: w.victims["another flow's run directory"]),
        False,
    ),
    "a directory below it, named like a run directory": (to(lambda w: w.victims["below"]), False),
    "the directory it lies in": (to(lambda w: w.parent), False),
    "the directory above it": (to(lambda w: w.design), False),
    "the run root": (to(lambda w: w.root), False),
    "a directory beside it with another name": (
        to(lambda w: w.victims["a directory with another name"]),
        False,
    ),
    "a directory beside it named almost like a run directory": (
        to(lambda w: w.victims["a name that is no hash"]),
        False,
    ),
    "a file": (to(lambda w: w.parent / "file.txt"), False),
    "nowhere": (to(lambda w: w.parent / "gone"), False),
    "itself": (to_itself, False),
    "a directory outside the run root": (to(lambda w: w.victims["outside the run root"]), False),
    "a chain that ends elsewhere": (to_a_link_that_leads_elsewhere, False),
    "a sibling run directory of the flow": (to(lambda w: w.sibling), True),
    "a chain that ends at a sibling run directory": (to_a_hop, True),
    "a real directory": (a_real_directory, True),
}
REFUSED = [name for name, (_, accepted) in CASES.items() if not accepted]
ACCEPTED = [name for name, (_, accepted) in CASES.items() if accepted]


def table(*names: str):
    return [
        pytest.param(name, hashed, id=f"{name}-{'hashed' if hashed else 'plain'}")
        for name in names
        for hashed in (False, True)
    ]


def made(tmp_path: Path, case: str, hashed: bool) -> tuple[World, Path]:
    world = World(tmp_path)
    link = world.link(hashed)
    CASES[case][0](world, link)
    return world, link


# ----------------------------------------------------------------------------- the one rule


def test_a_run_directory_is_named_like_the_flow_or_like_the_flow_and_a_hash():
    named = run_directory_name(FLOW)
    assert DIR_NAME_HASH_LEN == 16 == len(HASH)
    for ok in (FLOW, f"{FLOW}_{HASH}", f"{FLOW}_{'0' * 16}"):
        assert named.match(ok), ok
    for bad in (
        f"{FLOW}_",
        f"{FLOW}_other",
        f"{FLOW}_{HASH[:-1]}",
        f"{FLOW}_{HASH}0",
        f"{FLOW}_{HASH.upper()}",
        f"{FLOW}.x",
        f"x{FLOW}",
        "yosys_fpga",
        # `$` also matches before a final newline: the name is not a run directory's
        f"{FLOW}\n",
        f"{FLOW}_{HASH}\n",
        f"{FLOW} ",
    ):
        assert not named.match(bad), bad


@pytest.mark.parametrize("name", [FLOW, f"{FLOW}_{HASH}"])
def test_a_directory_named_with_a_final_newline_is_no_run_directory_and_scrub_leaves_it(
    tmp_path, confirmations, name
):
    world = World(tmp_path)
    odd = run_dir(world.parent / f"{name}\n")
    assert run_directory_problem(odd, FLOW, world.parent.resolve()) == (
        f"its name is not {FLOW} or {FLOW}_<hash>"
    )
    result = default_runner.scrub_design(FLOW, world.design, run_root=world.root, target="a")
    assert world.sibling in result.removed
    assert odd not in result.removed and (odd / "out.txt").exists()


@pytest.mark.parametrize(("case", "hashed"), table(*CASES))
def test_the_rule_says_why_a_link_is_none(tmp_path, case, hashed):
    world, link = made(tmp_path, case, hashed)
    problem = run_directory_problem(link, FLOW, world.parent.resolve())
    assert (problem is None) == CASES[case][1], problem
    if problem is not None:
        assert problem.startswith("it ")


def test_the_reasons_are_these(tmp_path):
    world = World(tmp_path)
    parent = world.parent.resolve()
    link = world.parent / FLOW
    for target, why in (
        (
            world.other_target,
            f"it is a link to {world.other_target.resolve()}, which is not a directory {RULE} "
            f"in {parent}",
        ),
        (
            world.parent / "file.txt",
            f"it is a link to {parent / 'file.txt'}, which is not a directory",
        ),
        (world.parent / "gone", f"it is a link to {parent / 'gone'}, which does not exist"),
    ):
        link.symlink_to(target)
        assert run_directory_problem(link, FLOW, parent) == why
        link.unlink()
    assert run_directory_problem(world.parent / "file.txt", FLOW, parent) == (
        f"its name is not {FLOW} or {FLOW}_<hash>"
    )
    link.write_text("a file, not a directory\n")
    assert run_directory_problem(link, FLOW, parent) == "it is not a directory"


def test_a_directory_is_judged_as_the_directory_it_resolves_to(tmp_path):
    """A real directory whose parent resolves elsewhere than the directory it was listed in is
    no run directory in that directory."""
    world = World(tmp_path)
    assert run_directory_problem(world.design / FLOW, FLOW, world.design.resolve()) is None
    problem = run_directory_problem(world.design / FLOW, FLOW, world.parent.resolve())
    assert problem == (
        f"it resolves to {(world.design / FLOW).resolve()}, which is not in {world.parent.resolve()}"
    )


# ------------------------------------------------------------ scrub and a launch agree on a link


@pytest.mark.parametrize(("case", "hashed"), table(*CASES))
def test_scrub_lists_a_link_if_and_only_if_a_launch_accepts_it(tmp_path, case, hashed):
    world, link = made(tmp_path, case, hashed)
    scan = default_runner._run_directories_in(FLOW, world.parent, (), world.root)
    listed = link in scan.candidates
    skipped = link in [s.link for s in scan.skipped]
    accepted = world.accepted_by_a_launch(hashed)
    assert listed == accepted, f"scrub lists it: {listed}, a launch accepts it: {accepted}"
    # a link is never both: it is listed, or it is reported (a real directory is only listed)
    assert not (listed and skipped)
    if link.is_symlink():
        assert listed or skipped
    assert accepted == CASES[case][1]


@pytest.mark.parametrize(("case", "hashed"), table(*REFUSED))
def test_a_launch_refuses_a_link_that_is_no_run_directory_and_writes_nothing(
    tmp_path, case, hashed
):
    world, link = made(tmp_path, case, hashed)
    before = entries(tmp_path)
    launcher = world.launcher(hashed)
    for refusal in (
        lambda: launcher.run_path_of("d", FLOW, HASH + "ffff" if hashed else None, target="a"),
        lambda: launcher.get_flow_run_path(
            "d", FLOW, HASH + "ffff" if hashed else None, target="a"
        ),
    ):
        with pytest.raises(RunDirectoryError) as refused:
            refusal()
        assert str(link) in str(refused.value)
    assert entries(tmp_path) == before, "nothing was written, not even a lock file"
    world.intact()


@pytest.mark.parametrize(("case", "hashed"), table(*ACCEPTED))
def test_a_launch_runs_in_a_link_that_leads_to_a_run_directory_beside_it(tmp_path, case, hashed):
    world, link = made(tmp_path, case, hashed)
    launcher = world.launcher(hashed)
    assert launcher.get_flow_run_path("d", FLOW, HASH + "ffff" if hashed else None, target="a") == (
        world.root / "d" / "a" / link.name
    )


def test_the_message_of_a_refused_launch(tmp_path):
    world, link = made(tmp_path, "a target's directory", False)
    with pytest.raises(RunDirectoryError) as refused:
        world.launcher(False).run_path_of("d", FLOW, target="a")
    assert str(refused.value) == (
        f"{link} cannot be the run directory of {FLOW}: it is a link to "
        f"{world.other_target.resolve()}, which is not a directory {RULE} in "
        f"{world.parent.resolve()}. Remove the link, or make it lead to a run directory of "
        f"{FLOW} beside it."
    )


def test_a_link_out_of_the_run_root_keeps_the_message_of_the_run_root(tmp_path):
    world, link = made(tmp_path, "a directory outside the run root", False)
    with pytest.raises(RunDirectoryError, match="leads out of the run root"):
        world.launcher(False).run_path_of("d", FLOW, target="a")


@pytest.mark.parametrize("case", ["a target's directory", "nowhere", "a file"])
def test_a_plan_refuses_the_link_a_launch_refuses(tmp_path, case):
    """A dry run reads the same rule: `plan` refuses before anything is written."""
    world = World(tmp_path)
    design_dir = tmp_path / "design"
    design_dir.mkdir()
    (design_dir / "d.yaml").write_text(
        'name: d\nrtl: {sources: [], top: "t"}\ntargets: {a: {}, b: {}}\n'
    )
    link = world.parent / _Maker.name
    CASES[case][0](world, link)
    before = entries(tmp_path)
    launcher = DefaultRunner(world.root, display_results=False)
    design = Design.from_file(design_dir / "d.yaml", target="a")
    with pytest.raises(RunDirectoryError) as refused:
        launcher.plan(_Maker, design)
    assert str(link) in str(refused.value)
    with pytest.raises(RunDirectoryError):
        launcher.launch_flow(_Maker, design, {})
    assert entries(tmp_path) == before


# ----------------------------------------------------------------------- scrub, end to end


@pytest.mark.parametrize(("case", "hashed"), table(*REFUSED))
def test_scrub_leaves_what_a_refused_link_leads_to_and_does_not_wait_for_its_lock(
    tmp_path, confirmations, said, case, hashed
):
    world, link = made(tmp_path, case, hashed)
    leads_to = Path(os.path.realpath(link))
    outcome: dict = {}

    def scrubbing():
        try:
            outcome["result"] = default_runner.scrub_design(
                FLOW, world.design, run_root=world.root, target="a"
            )
        except BaseException as error:  # noqa: BLE001 - reported by the test thread, below
            outcome["error"] = error

    # the run that lives where the link leads (a launch of it is running): its lock is held
    holding = (
        run_dir_lock(leads_to, world.root)
        if leads_to.exists() and RunDirectory.lies_under(leads_to, world.root)
        else contextlib.nullcontext()
    )
    with holding:
        thread = threading.Thread(target=scrubbing)
        thread.start()
        thread.join(timeout=30)
        assert not thread.is_alive(), "scrub waited for a lock that is not its run's"
    assert "error" not in outcome, repr(outcome.get("error"))
    result = outcome["result"]
    assert result.removed == [world.sibling], "only the run directory beside the links"
    assert link in result.skipped and link.is_symlink()
    world.intact()
    problem = run_directory_problem(link, FLOW, world.parent.resolve())
    assert f"skipped {link}: {problem}" in said


@pytest.mark.parametrize(("case", "hashed"), table(*ACCEPTED))
def test_scrub_removes_a_link_that_leads_to_a_run_directory_beside_it_and_that_directory(
    tmp_path, confirmations, case, hashed
):
    world, link = made(tmp_path, case, hashed)
    result = default_runner.scrub_design(FLOW, world.design, run_root=world.root, target="a")
    assert result.skipped == []
    assert not os.path.lexists(link) and not world.sibling.exists()
    world.intact()


# the failures that were found, as they were found


def test_a_link_to_a_target_s_directory_does_not_make_scrub_remove_the_runs_in_it(
    tmp_path, confirmations
):
    """`<design>/<flow>_<hash>` led to `<design>/a`: the other flows' runs in the target went."""
    world = World(tmp_path)
    inner = world.victims["another flow's run directory"]
    link = world.design / f"{FLOW}_{HASH}"
    link.symlink_to(world.parent, target_is_directory=True)
    result = default_runner.scrub_design(FLOW, world.design, run_root=world.root)
    assert link.is_symlink() and link in result.skipped
    assert (inner / "out.txt").exists() and (world.parent / "store" / "out.txt").exists()
    assert world.sibling not in result.skipped and not world.sibling.exists(), "its own"
    assert world.design / FLOW in result.removed


def test_scrub_does_not_wait_for_the_run_a_link_to_a_target_s_directory_leads_to(
    tmp_path, confirmations
):
    world = World(tmp_path)
    inner = world.victims["another flow's run directory"]
    (world.design / f"{FLOW}_{HASH}").symlink_to(world.parent, target_is_directory=True)
    outcome: dict = {}

    def scrubbing():
        outcome["result"] = default_runner.scrub_design(
            FLOW, world.design, run_root=world.root, target=None
        )

    with run_dir_lock(inner, world.root):  # a launch of yosys_fpga is running there
        thread = threading.Thread(target=scrubbing)
        thread.start()
        thread.join(timeout=30)
        assert not thread.is_alive(), "scrub waited for the running launch of another flow"
        assert inner.exists()
    assert (inner / "out.txt").read_text() == "output\n"


def test_a_link_to_another_flow_s_run_directory_in_a_target_is_not_scrubbed(
    tmp_path, confirmations
):
    world = World(tmp_path)
    inner = world.victims["another flow's run directory"]
    link = world.parent / f"{FLOW}_{HASH}"
    link.symlink_to(inner, target_is_directory=True)
    result = default_runner.scrub_design(FLOW, world.design, run_root=world.root, target="a")
    assert result.skipped == [link] and link.is_symlink()
    assert (inner / "out.txt").read_text() == "output\n"


# ----------------------------------------------------------------- a link changes during a scrub


@pytest.mark.parametrize(
    "becomes",
    [
        pytest.param(lambda w: w.other_target, id="a target's directory"),
        pytest.param(lambda w: w.victims["another flow's run directory"], id="another flow's run"),
        pytest.param(lambda w: w.victims["a directory with another name"], id="another name"),
        pytest.param(lambda w: w.victims["outside the run root"], id="outside"),
    ],
)
def test_a_link_retargeted_while_scrub_waits_for_its_lock_is_refused_and_nothing_is_removed(
    tmp_path, confirmations, monkeypatch, becomes
):
    world = World(tmp_path)
    link = world.link(True)
    link.symlink_to(world.sibling, target_is_directory=True)
    real_lock = default_runner.run_dir_lock
    after_the_change: list = []

    def retargeting(path, *args, **kwargs):
        if Path(path) == world.sibling and not after_the_change:  # the lock the link is held by
            link.unlink()
            link.symlink_to(becomes(world), target_is_directory=True)
            after_the_change.append(entries(tmp_path))
        return real_lock(path, *args, **kwargs)

    monkeypatch.setattr(default_runner, "run_dir_lock", retargeting)
    with pytest.raises(RunDirectoryError, match="changed while scrub waited"):
        default_runner.scrub_design(FLOW, world.design, run_root=world.root, target="a")
    assert after_the_change, "the lock of the directory the link led to was never asked for"
    assert [e for e in entries(tmp_path) if not e[0].endswith(".lock")] == [
        e for e in after_the_change[0] if not e[0].endswith(".lock")
    ], "something was removed"
    world.intact()


def test_a_directory_replaced_by_a_link_to_a_directory_with_another_name_after_it_was_listed_is_refused(
    tmp_path, monkeypatch
):
    """The listing saw a run directory. By the time scrub chooses its lock the path is a link to
    a directory beside it that no run directory is named like: scrub holds that directory's lock
    and still refuses to remove it, with the rule's reason."""
    world = World(tmp_path)
    victim = world.sibling
    store = world.victims["a directory with another name"]

    def ask(prompt="", *args, **kwargs):  # after the listing, before any lock is chosen
        RunDirectory.claimed(victim, world.root).delete()
        victim.symlink_to(store, target_is_directory=True)
        return "yes"

    monkeypatch.setattr(console, "input", ask)
    with pytest.raises(RunDirectoryError) as refused:
        default_runner.scrub_design(FLOW, world.design, run_root=world.root, target="a")
    assert str(refused.value) == (
        f"{victim} is no longer a run directory of {FLOW} in {world.parent.resolve()}: it is a "
        f"link to {store.resolve()}, which is not a directory {RULE} in "
        f"{world.parent.resolve()}. It was not removed."
    )
    assert (store / "out.txt").read_text() == "output\n"


# -------------------------------------------------------------------- what the user is told


def test_scrub_json_lists_the_links_it_skipped_and_says_why_on_stderr(tmp_path, monkeypatch):
    world = World(tmp_path)
    link = world.link(True)
    link.symlink_to(world.other_target, target_is_directory=True)
    monkeypatch.setattr(console, "input", lambda *a, **k: "yes")
    result = CliRunner().invoke(
        cli,
        ["scrub", FLOW, "d", "--target", "a", "--run-root", str(world.root), "--json"],
        catch_exceptions=False,
    )
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["skipped"] == [str(link)]
    assert document["scrubbed"] == [str(world.sibling)]
    assert document["kept"] == [] and document["gone"] == []
    assert link not in [Path(p) for p in document["scrubbed"]]
    assert (
        f"skipped {link}: it is a link to {world.other_target.resolve()}, which is not a "
        f"directory {RULE} in {world.parent.resolve()}"
    ) in result.stderr


def test_scrub_json_has_an_empty_skipped_list_when_there_is_none(tmp_path, monkeypatch):
    world = World(tmp_path)
    monkeypatch.setattr(console, "input", lambda *a, **k: "yes")
    result = CliRunner().invoke(
        cli,
        ["scrub", FLOW, "d", "--target", "a", "--run-root", str(world.root), "--json"],
        catch_exceptions=False,
    )
    assert json.loads(result.stdout)["skipped"] == []


def test_a_link_is_said_even_when_the_removal_is_declined_or_there_is_nothing_to_remove(
    tmp_path, monkeypatch, said
):
    world = World(tmp_path)
    RunDirectory.claimed(world.sibling, world.root).delete()
    link = world.link(False)
    link.symlink_to(world.design, target_is_directory=True)
    asked: list = []
    monkeypatch.setattr(console, "input", lambda *a, **k: asked.append(a) or "no")
    result = default_runner.scrub_design(FLOW, world.design, run_root=world.root, target="a")
    assert result.skipped == [link] and result.removed == [] and asked == []
    assert any(line.startswith(f"skipped {link}: ") for line in said)


def test_a_launch_s_scrub_says_a_link_it_skips(tmp_path, monkeypatch, said):
    world = World(tmp_path)
    link = world.link(True)
    link.symlink_to(world.victims["a directory with another name"], target_is_directory=True)
    monkeypatch.setattr(console, "input", lambda *a, **k: "yes")
    assert default_runner.scrub_runs(FLOW, world.parent, run_root=world.root) is True
    assert link.is_symlink() and not world.sibling.exists()
    assert any(line.startswith(f"skipped {link}: it is a link to ") for line in said)
    world.intact()


# ------------------------------------------------------------ one directory, several names

LAST_HASH = "ffffffffffffffff"  # sorts after OTHER_HASH, the name of the sibling directory


def a_directory_with_links(world: World, *links: str) -> list[Path]:
    """The sibling run directory, and a link to it at each of `links` (names in its directory)."""
    made = []
    for name in links:
        link = world.parent / name
        link.symlink_to(world.sibling, target_is_directory=True)
        made.append(link)
    return made


NAMES_OF_ONE_DIRECTORY = {
    "a link that sorts first": (FLOW,),
    "a hashed link that sorts first": (f"{FLOW}_{HASH}",),
    "a link that sorts last": (f"{FLOW}_{LAST_HASH}",),
    "two links": (FLOW, f"{FLOW}_{LAST_HASH}"),
}


@pytest.mark.parametrize("names", NAMES_OF_ONE_DIRECTORY.values(), ids=NAMES_OF_ONE_DIRECTORY)
def test_a_launch_s_scrub_removes_a_directory_and_every_link_to_it(tmp_path, confirmations, names):
    """`--scrub` lists a directory once, whatever it is called. It used to remove the directory
    and then fail on a link to it, which led nowhere by then, and the launch stopped."""
    world = World(tmp_path)
    links = a_directory_with_links(world, *names)
    assert default_runner.scrub_runs(FLOW, world.parent, run_root=world.root) is True
    assert not world.sibling.exists()
    assert not any(os.path.lexists(link) for link in links), "a link led nowhere"
    world.intact()


@pytest.mark.parametrize("names", NAMES_OF_ONE_DIRECTORY.values(), ids=NAMES_OF_ONE_DIRECTORY)
def test_scrub_leaves_no_link_that_leads_nowhere(tmp_path, confirmations, names):
    """Every name of a removed run directory goes with it. A link left behind would be refused
    by every later launch and reported as skipped by every later scrub, which could not remove it.
    """
    world = World(tmp_path)
    links = a_directory_with_links(world, *names)
    result = default_runner.scrub_design(FLOW, world.design, run_root=world.root, target="a")
    assert result.removed == [min([world.sibling, *links])], "one name for the directory"
    assert result.skipped == []
    assert not world.sibling.exists()
    assert not any(os.path.lexists(link) for link in links)
    again = default_runner.scrub_design(FLOW, world.design, run_root=world.root, target="a")
    assert again.removed == [] and again.skipped == []
    for hashed in (False, True):
        assert world.accepted_by_a_launch(hashed), "a launch may use the name again"
    world.intact()


def test_scrub_names_every_name_of_a_directory_before_it_asks(tmp_path, confirmations, said):
    world = World(tmp_path)
    links = a_directory_with_links(world, FLOW, f"{FLOW}_{LAST_HASH}")
    default_runner.scrub_design(FLOW, world.design, run_root=world.root, target="a")
    listing = "\n".join(said)
    assert all(str(name) in listing for name in (world.sibling, *links))
    assert "1 folders removed." in said[-1]


def test_a_name_that_changed_while_scrub_waited_for_the_lock_is_refused_and_nothing_is_removed(
    tmp_path, confirmations, monkeypatch
):
    world = World(tmp_path)
    (link,) = a_directory_with_links(world, f"{FLOW}_{LAST_HASH}")
    real_lock = default_runner.run_dir_lock
    after_the_change: list = []

    def retargeting(path, *args, **kwargs):
        if Path(path) == world.sibling and not after_the_change:
            link.unlink()
            link.symlink_to(world.other_target, target_is_directory=True)
            after_the_change.append(entries(tmp_path))
        return real_lock(path, *args, **kwargs)

    monkeypatch.setattr(default_runner, "run_dir_lock", retargeting)
    with pytest.raises(RunDirectoryError, match="changed while scrub waited"):
        default_runner.scrub_design(FLOW, world.design, run_root=world.root, target="a")
    assert after_the_change
    live = [e for e in entries(tmp_path) if not e[0].endswith(".lock")]
    assert live == [e for e in after_the_change[0] if not e[0].endswith(".lock")]
    world.intact()


def test_a_name_that_is_gone_when_scrub_holds_the_lock_is_no_error(
    tmp_path, confirmations, monkeypatch
):
    world = World(tmp_path)
    links = a_directory_with_links(world, FLOW, f"{FLOW}_{LAST_HASH}")
    real_lock = default_runner.run_dir_lock

    def removing(path, *args, **kwargs):
        if Path(path) == world.sibling and os.path.lexists(links[1]):
            links[1].unlink()  # another scrub, or the user
        return real_lock(path, *args, **kwargs)

    monkeypatch.setattr(default_runner, "run_dir_lock", removing)
    default_runner.scrub_design(FLOW, world.design, run_root=world.root, target="a")
    assert not world.sibling.exists() and not any(os.path.lexists(link) for link in links)


def test_a_directory_kept_because_a_run_ended_in_it_keeps_its_links(
    tmp_path, confirmations, monkeypatch, said
):
    world = World(tmp_path)
    links = a_directory_with_links(world, FLOW, f"{FLOW}_{LAST_HASH}")
    real_lock = default_runner.run_dir_lock

    def a_run_ends(path, *args, **kwargs):
        if Path(path) == world.sibling:
            (world.sibling / "results.json").write_text('{"success": true}\n')
        return real_lock(path, *args, **kwargs)

    monkeypatch.setattr(default_runner, "run_dir_lock", a_run_ends)
    result = default_runner.scrub_design(FLOW, world.design, run_root=world.root, target="a")
    assert result.kept == [links[0]] and result.removed == []
    assert world.sibling.exists() and all(link.is_symlink() for link in links)


def test_a_name_that_changed_is_refused_even_when_a_run_ended_in_the_directory(
    tmp_path, confirmations, monkeypatch
):
    """Every name of the directory is judged before its run records are, as the first name is,
    so a retargeted link is an error and not hidden behind "kept"."""
    world = World(tmp_path)
    links = a_directory_with_links(world, FLOW, f"{FLOW}_{LAST_HASH}")
    real_lock = default_runner.run_dir_lock

    def a_run_ends_and_a_name_changes(path, *args, **kwargs):
        if Path(path) == world.sibling:
            (world.sibling / "results.json").write_text('{"success": true}\n')
            links[1].unlink()
            links[1].symlink_to(world.other_target, target_is_directory=True)
        return real_lock(path, *args, **kwargs)

    monkeypatch.setattr(default_runner, "run_dir_lock", a_run_ends_and_a_name_changes)
    with pytest.raises(RunDirectoryError, match="changed while scrub waited"):
        default_runner.scrub_design(FLOW, world.design, run_root=world.root, target="a")
    assert world.sibling.exists() and all(link.is_symlink() for link in links)
    world.intact()


# --------------------------------------------------------------- a purge through a link


def maker_design(tmp_path: Path) -> Design:
    folder = tmp_path / "design"
    folder.mkdir(exist_ok=True)
    (folder / "d.yaml").write_text('name: d\nrtl: {sources: [], top: "t"}\n')
    return Design.from_file(folder / "d.yaml")


@pytest.mark.parametrize("option", ["post_cleanup_purge", "both"])
def test_a_purge_through_a_link_takes_the_link_with_the_directory(tmp_path, option):
    """`--post-cleanup-purge` of a run directory that is a link to a run directory beside it
    removes the directory, and the link: a link left leading nowhere would be refused by the
    next launch."""
    root = ensure_run_root(tmp_path / "xeda_run")
    target = run_dir(root / "d" / f"{_Maker.name}_cccccccccccccccc")
    link = root / "d" / _Maker.name
    link.symlink_to(target, target_is_directory=True)
    purge = {"post_cleanup_purge": True, "post_cleanup": option == "both"}
    launcher = DefaultRunner(root, display_results=False, **purge)
    flow = launcher.launch_flow(_Maker, maker_design(tmp_path), {})
    assert flow.succeeded
    assert not target.exists(), "the run directory was purged"
    assert not os.path.lexists(link), "the link led nowhere"
    again = DefaultRunner(root, display_results=False, **purge)
    assert again.launch_flow(_Maker, maker_design(tmp_path), {}).succeeded


@pytest.mark.parametrize("option", ["post_cleanup_purge", "both"])
def test_a_purge_takes_every_name_of_the_directory_with_it(tmp_path, option):
    """A run directory with two names beside it (the link a launch names it by, and another valid
    link to it): a purge removes both with the directory, as scrub does, so neither is left
    leading nowhere. A link to another directory is no name of it, and stays."""
    root = ensure_run_root(tmp_path / "xeda_run")
    target = run_dir(root / "d" / f"{_Maker.name}_cccccccccccccccc")
    link = root / "d" / _Maker.name
    link.symlink_to(target, target_is_directory=True)
    other_name = root / "d" / f"{_Maker.name}_dddddddddddddddd"
    other_name.symlink_to(target, target_is_directory=True)
    unrelated = run_dir(root / "d" / f"{_Maker.name}_eeeeeeeeeeeeeeee")
    elsewhere = root / "d" / f"{_Maker.name}_ffffffffffffffff"
    elsewhere.symlink_to(unrelated, target_is_directory=True)
    purge = {"post_cleanup_purge": True, "post_cleanup": option == "both"}
    launcher = DefaultRunner(root, display_results=False, **purge)
    assert launcher.launch_flow(_Maker, maker_design(tmp_path), {}).succeeded
    assert not target.exists(), "the run directory was purged"
    assert not os.path.lexists(link) and not os.path.lexists(other_name), "no name leads nowhere"
    assert unrelated.is_dir() and elsewhere.is_symlink(), "another directory's names stay"


def test_a_purge_of_an_explored_run_through_a_link_takes_the_link_with_the_directory(tmp_path):
    root = ensure_run_root(tmp_path / "xeda_run")
    target = run_dir(root / "d" / f"{FLOW}_cccccccccccccccc")
    link = root / "d" / FLOW
    link.symlink_to(target, target_is_directory=True)
    other_name = root / "d" / f"{FLOW}_dddddddddddddddd"
    other_name.symlink_to(target, target_is_directory=True)
    _purge_run(link, root, FLOW)
    assert not target.exists() and not os.path.lexists(link)
    assert not os.path.lexists(other_name), "every name goes with the directory"
