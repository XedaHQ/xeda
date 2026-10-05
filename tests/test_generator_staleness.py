"""A design generator runs again when what it reads or produced changed -- judged by content.

The decision is `xeda.generation`'s: the generator's configuration, its own sources' content and
the digest of every installed package it declares name a record under
`<run root>/.cache/generators/`, and that record holds the digest of every source its last
generation left. No modification time decides anything, so each of these tests would have the
wrong answer under a mtime rule: a stale source copied over a generated one with a newer mtime
(`cp -p`) is caught, an edit given back its old mtime is caught, and a plain `touch` of an input
costs a hash rather than a re-run.

A generator writes the design's own sources, at the paths the design declares: that is its job,
and the design's tree is not xeda's. What *xeda* keeps about it -- the record -- goes in the run
root, like every other thing of xeda's (D21); `test_isolation.py` holds the oracle for that.
"""

import os
import shutil
import sys
import time
from contextlib import nullcontext
from pathlib import Path
from typing import Optional

import pytest

from xeda import Design
from xeda.design import DesignValidationError, loading_in_run_root, refusing_load_side_effects
from xeda.digest import installed_package_digest
from xeda.generation import CACHE_DIRECTORY
from xeda.run_dir import RunDirectoryError
from xeda.run_root import ensure_run_root
from xeda.utils import NonZeroExitCode

#: Writes `gen/top.v` from `spec.txt`, and appends a line to the file named by its first argument
#: (a run counter, outside the design tree). With `site/` beside the design it imports the package
#: there first, as litex's SoC script imports litex: its own content does not depend on it.
GENERATOR = """\
import os, sys
from pathlib import Path

root = Path(os.environ["DESIGN_ROOT"])
site = root / "site"
if site.is_dir():
    sys.path.insert(0, str(site))
    import generated_from  # noqa: F401
(root / "gen").mkdir(exist_ok=True)
(root / "gen" / "top.v").write_text("// " + (root / "spec.txt").read_text())
if len(sys.argv) > 1:
    with open(sys.argv[1], "a") as counter:
        counter.write("ran\\n")
if (root / "fail").exists():
    raise SystemExit(1)
"""


def _yaml(value) -> str:
    """The value as YAML writes it: a boolean in lower case, everything else as Python spells it
    (a quoted string, a flow sequence of them)."""
    return repr(value) if not isinstance(value, bool) else str(value).lower()


class World:
    """A design whose sources a generator writes, and a marked run root beside it."""

    def __init__(self, tmp_path: Path, **generator) -> None:
        self.root = tmp_path / "design"
        self.root.mkdir(exist_ok=True)
        self.counter = tmp_path / "runs.log"
        self.run_root = tmp_path / "xeda_run"
        (self.root / "gen.py").write_text(GENERATOR)
        (self.root / "spec.txt").write_text("one\n")
        generator = {
            "executable": sys.executable,
            "args": ["gen.py", str(self.counter)],
            "sources": ["spec.txt", "gen.py"],
            **generator,
        }
        self.design_file = self.root / "design.yaml"
        self.design_file.write_text(
            "name: generated\n"
            "rtl:\n"
            "  sources: [gen/top.v]\n"
            "  top: top\n"
            "  generator:\n"
            + "".join(f"    {key}: {_yaml(value)}\n" for key, value in generator.items())
        )
        self.generated = self.root / "gen" / "top.v"

    @property
    def runs(self) -> int:
        """How many times the generator has run."""
        return len(self.counter.read_text().splitlines()) if self.counter.exists() else 0

    def load(self, planning: bool = False, run_root: bool = True) -> Design:
        """Load the design as a launcher does: with the run root a load keeps its records in."""

        def provider(create: bool) -> Optional[Path]:
            return ensure_run_root(self.run_root, start=self.root, create=create)

        with loading_in_run_root(provider) if run_root else nullcontext():
            if planning:
                with refusing_load_side_effects():
                    return Design.from_file(self.design_file)
            return Design.from_file(self.design_file)

    def records(self) -> list[Path]:
        cache = self.run_root / CACHE_DIRECTORY
        return sorted(cache.glob("*.yaml")) if cache.is_dir() else []


@pytest.fixture(autouse=True)
def _a_fresh_process():
    """Every test is a separate `xeda` invocation: a package digest is cached per process, by its
    name, so one test's package must not answer for another's."""
    installed_package_digest.cache_clear()
    yield
    installed_package_digest.cache_clear()


def _restore_times(path: Path, times: os.stat_result) -> None:
    """Give a file back the timestamps it had, as `touch -r` and `cp -p` do."""
    os.utime(path, ns=(times.st_atime_ns, times.st_mtime_ns))


def _newest(path: Path) -> None:
    """Make `path` the newest file around, which is all a mtime rule ever asked of an output."""
    now = time.time() + 10
    os.utime(path, (now, now))


# --- the content rule, where a modification time gives the wrong answer ---------------------


def test_a_generator_runs_once_and_not_again_while_nothing_changed(tmp_path):
    world = World(tmp_path)
    design = world.load()
    assert world.runs == 1
    assert [src.file.name for src in design.rtl.sources] == ["top.v"]
    assert len(world.records()) == 1
    world.load()
    world.load()
    assert world.runs == 1, "a load that need not generate anything generated"


def test_a_stale_source_copied_over_a_generated_one_runs_the_generator(tmp_path):
    """`cp -p` of a stale output, which keeps its own (newer) modification time: a mtime rule
    sees the newest file of all in the right place and skips the generation it owes."""
    world = World(tmp_path)
    world.load()
    stale = tmp_path / "stale.v"
    stale.write_text("// an older generation\n")
    _newest(stale)
    shutil.copy2(stale, world.generated)  # cp -p: the stale content with its newer mtime
    assert world.generated.stat().st_mtime > (world.root / "spec.txt").stat().st_mtime
    world.load()
    assert world.runs == 2
    assert world.generated.read_text() == "// one\n"


def test_an_edit_given_back_its_old_mtime_runs_the_generator(tmp_path):
    """The inode change time moves whatever is done to the modification time, and the content
    decides anyway: an edited input is an input that changed."""
    world = World(tmp_path)
    world.load()
    spec = world.root / "spec.txt"
    times = spec.stat()
    spec.write_text("two\n")
    _restore_times(spec, times)
    assert spec.stat().st_mtime == times.st_mtime
    world.load()
    assert world.runs == 2
    assert world.generated.read_text() == "// two\n"


def test_touching_a_generator_source_does_not_run_it_again(tmp_path):
    """A `touch`, a `chmod`, a branch round-trip: the content is what is read, so this costs a
    hash, never a re-run. A mtime rule runs the generator again on every one of them."""
    world = World(tmp_path)
    world.load()
    spec = world.root / "spec.txt"
    _newest(spec)
    assert spec.stat().st_mtime > world.generated.stat().st_mtime
    world.load()
    assert world.runs == 1


def test_a_generated_source_that_was_deleted_runs_the_generator(tmp_path):
    world = World(tmp_path)
    world.load()
    world.generated.unlink()
    world.load()
    assert world.runs == 2


def test_a_changed_generator_configuration_runs_it_again(tmp_path):
    world = World(tmp_path)
    world.load()
    world.design_file.write_text(
        world.design_file.read_text().replace("    args:", "    check: true\n    args:")
    )
    world.load()
    assert world.runs == 1, "a setting written as its own default is the same configuration"
    world.design_file.write_text(world.design_file.read_text().replace("check: true", "cwd: '.'"))
    world.load()
    assert world.runs == 2
    assert len(world.records()) == 2, "another configuration is another record"


def test_the_environment_the_generator_inherits_is_not_part_of_the_record(tmp_path, monkeypatch):
    """`process_generation` completes a generator with this shell's whole environment before it
    runs it; a record named by that would be reusable from no other shell, and xeda tracks no
    environment variable anywhere."""
    world = World(tmp_path)
    world.load()
    monkeypatch.setenv("XEDA_TESTS_UNRELATED", "whatever")
    world.load()
    monkeypatch.delenv("XEDA_TESTS_UNRELATED")
    world.load()
    assert world.runs == 1


# --- an input that cannot be listed as files: an installed Python package -------------------


def _installed_package(tmp_path: Path, monkeypatch, body: str = "VERSION = 1\n") -> Path:
    """A package installed where this interpreter finds it, as litex is in a virtual environment,
    and where the generator's own `sys.path` finds it too (`site/` beside the design)."""
    site = tmp_path / "design" / "site"
    package = site / "generated_from"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(body)
    monkeypatch.syspath_prepend(str(site))
    import importlib

    importlib.invalidate_caches()
    return package


def _another_process() -> None:
    """A package is digested once per process (`installed_package_digest`, as the installed xeda
    package is): what a second `xeda` invocation would see, this clears."""
    installed_package_digest.cache_clear()


def test_a_change_to_an_installed_package_the_generator_reads_runs_it_again(tmp_path, monkeypatch):
    package = _installed_package(tmp_path, monkeypatch)
    world = World(tmp_path, packages=["generated_from"])
    world.load()
    assert world.runs == 1
    _another_process()
    world.load()
    assert world.runs == 1, "an unchanged package is not a change"
    (package / "__init__.py").write_text("VERSION = 1  # the upgrade changed only this comment\n")
    _another_process()
    world.load()
    assert world.runs == 2, "a package the generator reads changed, and it generated the same file"
    assert world.generated.read_text() == "// one\n"


def test_a_file_added_to_an_installed_package_runs_the_generator_again(tmp_path, monkeypatch):
    package = _installed_package(tmp_path, monkeypatch)
    world = World(tmp_path, packages=["generated_from"])
    world.load()
    (package / "platforms.py").write_text("BOARDS = ['arty']\n")
    _another_process()
    world.load()
    assert world.runs == 2


def test_touching_an_installed_package_does_not_run_the_generator_again(tmp_path, monkeypatch):
    package = _installed_package(tmp_path, monkeypatch)
    world = World(tmp_path, packages=["generated_from"])
    world.load()
    _newest(package / "__init__.py")
    _another_process()
    world.load()
    assert world.runs == 1


def test_bytecode_of_an_installed_package_is_not_part_of_the_decision(tmp_path, monkeypatch):
    """Python writes `__pycache__` wherever it imports from; it is not the package's content."""
    package = _installed_package(tmp_path, monkeypatch)
    world = World(tmp_path, packages=["generated_from"])
    world.load()
    cache = package / "__pycache__"
    cache.mkdir(exist_ok=True)
    (cache / "whatever.cpython-313.pyc").write_bytes(b"\x00bytecode")
    _another_process()
    world.load()
    assert world.runs == 1


def test_a_package_nothing_provides_is_named_as_the_error_it_is(tmp_path):
    world = World(tmp_path, packages=["no_such_package_anywhere"])
    with pytest.raises(DesignValidationError, match="no_such_package_anywhere"):
        world.load()
    assert world.runs == 0


# --- what cannot be judged runs, as everywhere else in xeda ---------------------------------


def test_a_generator_that_declares_always_runs_runs_on_every_load(tmp_path):
    world = World(tmp_path, always_runs=True)
    world.load()
    world.load()
    assert world.runs == 2
    assert world.records() == [], "a generator that is never judged is never recorded"


def test_a_generator_with_nothing_to_judge_it_by_runs_on_every_load(tmp_path):
    world = World(tmp_path, sources=[])
    world.load()
    world.load()
    assert world.runs == 2
    assert world.records() == []


def test_without_a_run_root_a_generator_runs_on_every_load(tmp_path):
    """A `Design` built or loaded outside a launcher has nowhere to keep a record, so it cannot
    prove the generated sources are up to date."""
    world = World(tmp_path)
    world.load(run_root=False)
    world.load(run_root=False)
    assert world.runs == 2
    assert not world.run_root.exists()


def test_a_run_root_that_cannot_be_written_leaves_the_generator_running(tmp_path):
    """Neither the record nor the lock beside it can be made, and a design load is not a place
    to fail over that: the generator runs, as it does wherever nothing can vouch for it."""
    world = World(tmp_path)
    world.load()
    cache = world.run_root / CACHE_DIRECTORY
    mode = cache.stat().st_mode
    for kept in sorted(cache.iterdir()):
        kept.unlink()
    os.chmod(cache, 0o500)
    try:
        world.load()
        world.load()
        assert world.runs == 3
        assert list(cache.iterdir()) == []
    finally:
        os.chmod(cache, mode)


def test_run_only_if_sources_modified_was_removed(tmp_path):
    world = World(tmp_path, run_only_if_sources_modified=False)
    with pytest.raises(DesignValidationError) as error:
        world.load()
    assert "`run_only_if_sources_modified` was removed: use `always_runs`" in str(error.value)
    assert world.runs == 0


# --- a plan creates nothing, records nothing and generates nothing ---------------------------


def test_a_plan_of_a_design_whose_generation_is_up_to_date_changes_nothing(tmp_path):
    world = World(tmp_path)
    world.load()
    [record] = world.records()
    before = (record.read_bytes(), sorted(p.name for p in world.run_root.rglob("*")))
    world.load(planning=True)
    assert world.runs == 1
    assert (record.read_bytes(), sorted(p.name for p in world.run_root.rglob("*"))) == before


def test_a_plan_of_a_design_that_must_generate_is_refused_and_creates_no_run_root(tmp_path):
    world = World(tmp_path)
    with pytest.raises(DesignValidationError, match="Cannot plan a design that needs a generator"):
        world.load(planning=True)
    assert world.runs == 0
    assert not world.run_root.exists(), "a plan created the run root"


# --- only what the generator produces is judged ----------------------------------------------


def test_only_the_sources_the_generator_declares_it_generates_are_judged(tmp_path):
    """A design whose `rtl.sources` hold hand-written files too names what the generator writes
    in `generated_sources`; editing one of its own sources is then not a reason to generate."""
    world = World(tmp_path, generated_sources=["gen/top.v"])
    (world.root / "mine.v").write_text("// mine\n")
    world.design_file.write_text(
        world.design_file.read_text().replace("[gen/top.v]", "[gen/top.v, mine.v]")
    )
    world.load()
    assert world.runs == 1
    (world.root / "mine.v").write_text("// mine, edited\n")
    world.load()
    assert world.runs == 1
    world.generated.write_text("// not what it left\n")
    world.load()
    assert world.runs == 2


def test_without_generated_sources_every_declared_source_is_judged(tmp_path):
    world = World(tmp_path)
    (world.root / "mine.v").write_text("// mine\n")
    world.design_file.write_text(
        world.design_file.read_text().replace("[gen/top.v]", "[gen/top.v, mine.v]")
    )
    world.load()
    assert world.runs == 1
    (world.root / "mine.v").write_text("// mine, edited\n")
    world.load()
    assert world.runs == 2, "nothing says which sources the generator owns, so all of them count"


def test_a_pattern_the_generator_fills_is_judged_by_what_it_matches(tmp_path):
    world = World(tmp_path)
    world.design_file.write_text(
        world.design_file.read_text().replace("[gen/top.v]", "['gen/*.v']")
    )
    world.load()
    assert world.runs == 1
    world.load()
    assert world.runs == 1
    (world.root / "gen" / "extra.v").write_text("// another file the pattern matches\n")
    world.load()
    assert world.runs == 2, "the pattern names another set of sources than the record holds"


# --- the record itself ------------------------------------------------------------------------


def test_the_record_lies_in_the_run_root_and_nothing_is_written_beside_the_design(tmp_path):
    world = World(tmp_path)
    world.load()
    [record] = world.records()
    assert record.is_relative_to(world.run_root / CACHE_DIRECTORY)
    assert record.with_suffix(".yaml.lock").exists() or sys.platform == "win32"
    assert sorted(p.name for p in world.root.rglob("*")) == [
        "design.yaml",
        "gen",
        "gen.py",
        "spec.txt",
        "top.v",
    ]


def test_a_record_of_another_format_or_a_damaged_one_runs_the_generator(tmp_path):
    world = World(tmp_path)
    world.load()
    [record] = world.records()
    record.write_text("format: 999\n")
    world.load()
    assert world.runs == 2
    record.write_text(": not yaml :\n[")
    world.load()
    assert world.runs == 3


def test_a_record_is_never_reached_through_a_link(tmp_path):
    world = World(tmp_path)
    world.load()
    cache = world.run_root / CACHE_DIRECTORY
    elsewhere = world.run_root / "elsewhere"
    elsewhere.mkdir()
    for kept in sorted(cache.iterdir()):
        kept.unlink()
    cache.rmdir()
    cache.symlink_to(elsewhere)
    # Not a design error: the run root is xeda's own, and the message says what to do about it.
    with pytest.raises(RunDirectoryError, match="symbolic link where xeda keeps a cache"):
        world.load()


# --- a directory a generator reads is judged by what is in it ---------------------------------


def test_a_directory_the_generator_reads_is_judged_by_its_files(tmp_path):
    """A source may name a directory -- a Chisel `src/main/scala`, a directory of templates. Its
    own digest is a constant, so it is expanded entry by entry, as the trace does for a directory
    a setting names: an edit inside it, or a file added to it, runs the generator again."""
    world = World(tmp_path, sources=["spec.txt", "gen.py", "templates"])
    templates = world.root / "templates"
    templates.mkdir()
    (templates / "top.v.in").write_text("// a template\n")
    world.load()
    assert world.runs == 1
    world.load()
    assert world.runs == 1
    (templates / "top.v.in").write_text("// an edited template\n")
    world.load()
    assert world.runs == 2
    (templates / "another.v.in").write_text("// one more\n")
    world.load()
    assert world.runs == 3
    _newest(templates / "another.v.in")
    world.load()
    assert world.runs == 3, "a touch inside the directory costs a hash, not a run"


def test_a_generator_source_that_vanished_is_named(tmp_path):
    """Between the design's validation and the hash, a source can go: its own name is the error,
    not an errno from inside a digest."""
    world = World(tmp_path)
    world.load()
    (world.root / "spec.txt").unlink()
    with pytest.raises(DesignValidationError, match="spec.txt"):
        world.load()


# --- the run root is made to record a generation, never before it -----------------------------


def test_a_generator_that_fails_leaves_no_run_root(tmp_path):
    """Asking whether a record exists must not make the run root, and a failed generation must
    not leave one: a design that fails to load leaves nothing behind."""
    world = World(tmp_path)
    (world.root / "fail").write_text("")
    with pytest.raises(NonZeroExitCode):
        world.load()
    assert world.runs == 1
    assert not world.run_root.exists()


def test_the_run_root_is_made_by_the_first_generation_that_succeeds(tmp_path):
    world = World(tmp_path)
    assert not world.run_root.exists()
    world.load()
    assert world.run_root.is_dir() and len(world.records()) == 1
    world.load()
    assert world.runs == 1


# --- the real entry point ---------------------------------------------------------------------


def test_a_launcher_generates_once_across_two_launches(tmp_path, monkeypatch):
    """The product's own path: `DefaultRunner` loads the design inside `loading_in_run_root`, so
    two launches of the same unchanged design run the generator once. The flow's own outcome is
    beside the point -- the count is settled while the design loads."""
    from xeda.flow_runner import DefaultRunner

    from .tool_utils import use_fake_tools

    use_fake_tools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    world = World(tmp_path)
    world.design_file.write_text(
        world.design_file.read_text().replace("  top: top\n", "  top: top\n  clock: {port: clk}\n")
    )
    settings = {"fpga": {"part": "xc7a35ticsg324-1L"}, "clock_period": 10.0}
    for _ in range(2):
        flow = DefaultRunner(world.run_root, display_results=False).run(
            "vivado_synth", world.design_file, flow_settings=settings
        )
        assert flow is not None and flow.succeeded
    assert world.runs == 1
    assert len(world.records()) == 1


def test_a_dangling_link_in_a_directory_source_is_a_file_like_any_other(tmp_path):
    """An editor's lock file (`.#Top.scala`) is a link to nothing, and it is there exactly while
    the file is open: a design must still load. An entry of a directory is recorded as itself,
    as the trace records a listing, so a link is its target text and never followed."""
    world = World(tmp_path, sources=["spec.txt", "gen.py", "templates"])
    templates = world.root / "templates"
    templates.mkdir()
    (templates / "top.v.in").write_text("// a template\n")
    (templates / ".#top.v.in").symlink_to("nowhere/at/all")
    world.load()
    assert world.runs == 1
    world.load()
    assert world.runs == 1
    (templates / ".#top.v.in").unlink()
    (templates / ".#top.v.in").symlink_to("somewhere/else")
    world.load()
    assert world.runs == 2, "the link names something else, which is a change like any other"


def test_rebuild_all_runs_the_generator_and_records_what_it_leaves(tmp_path, monkeypatch):
    """`--rebuild-all` (and `--clean`, which implies it) is what forces everything to run again,
    a generation included -- there is no `touch` to fall back on any more. The record is still
    written, so the next ordinary launch is up to date."""
    from xeda.flow_runner import DefaultRunner

    from .tool_utils import use_fake_tools

    use_fake_tools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    world = World(tmp_path)
    world.design_file.write_text(
        world.design_file.read_text().replace("  top: top\n", "  top: top\n  clock: {port: clk}\n")
    )
    settings = {"fpga": {"part": "xc7a35ticsg324-1L"}, "clock_period": 10.0}

    def launch(**launcher) -> None:
        runner = DefaultRunner(world.run_root, display_results=False, **launcher)
        flow = runner.run("vivado_synth", world.design_file, flow_settings=settings)
        assert flow is not None and flow.succeeded

    launch()
    assert world.runs == 1
    launch()
    assert world.runs == 1
    launch(rebuild_all=True)
    assert world.runs == 2
    launch(clean=True)
    assert world.runs == 3
    launch()
    assert world.runs == 3, "the record of the last generation is there again"
