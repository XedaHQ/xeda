"""A symbolic link between the run root and a run directory is refused.

A run directory is `<run root>/<design>[/<target>]/<flow>[_<hash>]`. Xeda neither follows nor
removes a link at the design or the target. One rule serves the launch (at planning, so also
`--dry-run`), `xeda scrub` and the locks, which the mirror of a remote run takes before it clears
anything (`link_on_way`). The oracles:

- the error names the link, whether the link leads into the run root or out of it;
- nothing is written or deleted on either side of the link: a sentinel file survives, and no
  lock file appears;
- a run root that is itself a link keeps working, for a launch, `--clean` and scrub;
- the rule for the run directory itself (`run_directory_problem`) is another test's.
"""

import json
import os
import sys
from pathlib import Path
from typing import ClassVar

import pytest
from click.testing import CliRunner

from xeda.flow import MetricSpec
from xeda import Design
from xeda.cli import cli
from xeda.console import console
from xeda.flow import Flow
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.run_lock import run_dir_lock, run_dir_read_lock
from xeda.run_dir import RunDirectory, RunDirectoryError, link_on_way
from xeda.run_root import ensure_run_root

from .flow_registry import preserved_flow_registry

pytestmark = [
    pytest.mark.python_compat,
    pytest.mark.skipif(sys.platform == "win32", reason="POSIX symbolic links and locks"),
]

SCRUBBED = "yosys_fpga"


class _Probe(Flow):
    """Does nothing: what matters is where it would run."""

    results_description: ClassVar[dict[str, MetricSpec]] = {}

    def run(self) -> None:
        pass


@pytest.fixture(autouse=True)
def private_registry():
    with preserved_flow_registry():
        yield


@pytest.fixture
def confirmed(monkeypatch):
    monkeypatch.setattr(console, "input", lambda *args, **kwargs: "yes")


def snapshot(root: Path) -> dict[str, str]:
    """Every name under `root` (a link as itself, never followed), with its kind or content."""
    found: dict[str, str] = {}
    for directory, directories, files in os.walk(root, followlinks=False):
        for name in (*directories, *files):
            path = Path(directory, name)
            relative = str(path.relative_to(root))
            if path.is_symlink():
                found[relative] = f"-> {os.readlink(path)}"
            elif path.is_dir():
                found[relative] = "dir"
            else:
                found[relative] = path.read_text()
    return found


DESIGN_FILE = """\
name: {name}
rtl:
  sources: [top.v]
  top: top
flows:
  yosys_fpga:
    fpga.part: LFE5U-25F-6BG381C
targets:
  t1:
    flows.yosys_fpga.fpga.part: LFE5U-25F-6BG381C
"""


class World:
    """A run root with a link at the design or the target level.

    `b` in the run root and `outside` beside it each hold `KEEP` in a run directory of every flow
    that the tests launch or scrub. The link leads to one of them."""

    def __init__(self, tmp_path: Path, case: str) -> None:
        self.base = tmp_path
        root = ensure_run_root(tmp_path / "xeda_run")
        assert root is not None
        self.root = root
        self.inside = root / "b"
        self.outside = tmp_path / "outside"
        for home in (self.inside, self.outside):
            for flow in (_Probe.name, SCRUBBED):
                (home / flow).mkdir(parents=True)
                (home / flow / "KEEP").write_text("keep\n")
        level, leads = case.split("-")
        self.target = "t1" if level == "target" else None
        self.design_name = "c" if level == "target" else "a"
        self.leads_to = self.inside if leads == "inside" else self.outside
        value = "../b" if (level, leads) == ("target", "inside") else str(self.leads_to)
        if (level, leads) == ("design", "inside"):
            value = "b"
        self.link = root / self.design_name
        if level == "target":
            self.link.mkdir()
            self.link = self.link / "t1"
        self.link.symlink_to(value, target_is_directory=True)
        self.design_file = tmp_path / "design" / "top.yaml"
        self.design_file.parent.mkdir()
        (self.design_file.parent / "top.v").write_text("module top(input a); endmodule\n")
        self.design_file.write_text(DESIGN_FILE.format(name=self.design_name))
        self.before = snapshot(tmp_path)

    def design(self) -> Design:
        return Design(
            name=self.design_name,
            target=self.target,
            design_root=self.base,
            rtl={"sources": []},
        )

    def unchanged(self) -> None:
        assert snapshot(self.base) == self.before, "something was written or deleted"


CASES = ["design-inside", "design-outside", "target-inside", "target-outside"]


def names_the_link(world: World, text: str) -> None:
    assert f"{world.link} is a symbolic link (to {os.readlink(world.link)})" in text, text
    assert "neither follows nor removes it" in text and "--run-root" in text, text


# ------------------------------------------------------------------------------- the launch


@pytest.mark.parametrize("clean", [False, True], ids=["plain", "clean"])
@pytest.mark.parametrize("case", CASES)
def test_a_launch_through_a_link_is_refused_before_anything_is_written(tmp_path, case, clean):
    world = World(tmp_path, case)
    runner = DefaultRunner(world.root, display_results=False, clean=clean)
    with pytest.raises(RunDirectoryError) as refused:
        runner.run_flow(_Probe, world.design(), {})
    names_the_link(world, str(refused.value))
    world.unchanged()


@pytest.mark.parametrize("arguments", [["--dry-run"], ["--clean"], []], ids=lambda a: "".join(a))
@pytest.mark.parametrize("case", CASES)
def test_the_command_line_refuses_a_link_with_and_without_a_dry_run(tmp_path, case, arguments):
    world = World(tmp_path, case)
    target = ["--target", world.target] if world.target else []
    result = CliRunner().invoke(
        cli,
        ["run", SCRUBBED, str(world.design_file), "--run-root", str(world.root), *target]
        + ["--json", *arguments],
    )
    assert result.exit_code == 1, result.output
    document = json.loads(result.stdout)
    assert document["success"] is False
    names_the_link(world, document["error"]["message"])
    world.unchanged()


# --------------------------------------------------------------------------------- the lock


@pytest.mark.parametrize("lock", [run_dir_lock, run_dir_read_lock])
@pytest.mark.parametrize("case", CASES)
def test_a_lock_through_a_link_is_refused_and_creates_nothing(tmp_path, case, lock):
    world = World(tmp_path, case)
    with pytest.raises(RunDirectoryError) as refused:
        with lock(world.link / SCRUBBED, world.root):
            pytest.fail("locked")
    names_the_link(world, str(refused.value))
    world.unchanged()


@pytest.mark.parametrize("case", CASES)
def test_a_claim_through_a_link_is_refused(tmp_path, case):
    world = World(tmp_path, case)
    with pytest.raises(RunDirectoryError) as refused:
        RunDirectory.claimed(world.link / SCRUBBED, world.root)
    names_the_link(world, str(refused.value))


def test_the_link_is_found_between_the_resolved_run_root_and_the_directory(tmp_path):
    world = World(tmp_path, "target-inside")
    assert link_on_way(world.link / SCRUBBED, world.root) == world.link
    assert link_on_way(world.link, world.root) == world.link, "the directory is on the way"
    assert link_on_way(world.root / "b", world.root) is None
    assert link_on_way(world.root, world.root) is None
    assert link_on_way(tmp_path / "elsewhere" / "x", world.root) is None
    # Another name for the run root: the way starts below it, whichever name a caller gives.
    alias = tmp_path / "alias"
    alias.symlink_to(world.root.parent, target_is_directory=True)
    through = alias / world.root.name / world.link.relative_to(world.root)
    assert link_on_way(through / SCRUBBED, world.root) == through
    assert link_on_way(world.link / SCRUBBED, alias / world.root.name) == world.link
    assert link_on_way(alias / world.root.name / "b", world.root) is None


# ---------------------------------------------------------------------------------- scrub


@pytest.mark.parametrize("case", CASES)
def test_a_scrub_through_a_link_is_refused_naming_it(tmp_path, case, confirmed):
    world = World(tmp_path, case)
    target = ["--target", world.target] if world.target else []
    result = CliRunner().invoke(
        cli,
        ["scrub", SCRUBBED, world.design_name, "--run-root", str(world.root), "--json", *target],
    )
    assert result.exit_code == 1, result.output
    names_the_link(world, json.loads(result.stdout)["error"]["message"])
    world.unchanged()


@pytest.mark.parametrize("leads", ["inside", "outside"])
def test_a_scrub_of_a_design_says_which_target_link_it_skipped(tmp_path, leads, confirmed):
    world = World(tmp_path, f"target-{leads}")
    real = world.root / "c" / "real"
    (real / SCRUBBED).mkdir(parents=True)
    for name in ("settings.json", "results.json", "trace.json"):
        (real / SCRUBBED / name).write_text("{}")
    world.before = snapshot(tmp_path)
    result = CliRunner().invoke(
        cli, ["scrub", SCRUBBED, "c", "--run-root", str(world.root), "--json"]
    )
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    assert document["skipped"] == [str(world.link)]
    assert document["scrubbed"] == [str(real / SCRUBBED)]
    after = snapshot(tmp_path)
    assert {k: v for k, v in after.items() if k.startswith("outside")} == {
        k: v for k, v in world.before.items() if k.startswith("outside")
    }
    assert (world.inside / SCRUBBED / "KEEP").read_text() == "keep\n"
    assert (world.outside / SCRUBBED / "KEEP").read_text() == "keep\n"
    assert world.link.is_symlink()


# ----------------------------------------------------------- a run root that is a link


def test_a_run_root_that_is_a_link_keeps_working(tmp_path, confirmed):
    real = tmp_path / "disk" / "xeda_run"
    real.parent.mkdir()
    link = tmp_path / "xeda_run"
    link.symlink_to(real, target_is_directory=True)
    design = Design(name="d", design_root=tmp_path, rtl={"sources": []})
    for clean in (False, True, True):
        flow = DefaultRunner(link, display_results=False, clean=clean).run_flow(_Probe, design, {})
        assert flow.run_path == real.resolve() / "d" / _Probe.name
    assert (real / "d" / _Probe.name / "results.json").is_file()
    assert link_on_way(link / "d", link) is None
    # scrub through the link names the same directories
    scrubbed = real / "d" / SCRUBBED
    scrubbed.mkdir()
    for name in ("settings.json", "results.json", "trace.json"):
        (scrubbed / name).write_text("{}")
    result = CliRunner().invoke(
        cli, ["scrub", SCRUBBED, "d", "--run-root", str(link), "--json"], catch_exceptions=False
    )
    assert result.exit_code == 0, result.output
    assert json.loads(result.stdout)["scrubbed"] == [str(scrubbed)]
    assert not scrubbed.exists()
