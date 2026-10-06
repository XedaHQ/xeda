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
root, like every other thing of xeda's; `test_isolation.py` holds the oracle for that.
"""

import os
import shutil
import subprocess
import sys
import time
import yaml
from contextlib import nullcontext
from pathlib import Path
from typing import Optional

import pytest

from xeda import Design
from xeda.design import DesignValidationError, loading_in_run_root, refusing_load_side_effects
from xeda.generation import CACHE_DIRECTORY
from xeda.run_dir import RunDirectoryError
from xeda.run_root import ensure_run_root
from xeda.utils import NonZeroExitCode

#: Writes `gen/top.v` from `spec.txt`, and appends a line to the file named by its first argument
#: (a run counter, outside the design tree). A second argument, when not empty, is a directory it
#: puts first on `sys.path` before importing `generated_from` from it, as litex's SoC script
#: imports litex (without one, `PYTHONPATH` in its environment does the same): the package's
#: version goes into the output. It writes no bytecode unless a third argument says `bytecode`.
GENERATOR = """\
import os, sys
from pathlib import Path

root = Path(os.environ["DESIGN_ROOT"])
sys.dont_write_bytecode = "bytecode" not in sys.argv[3:]
lib = sys.argv[2] if len(sys.argv) > 2 else ""
if lib:
    sys.path.insert(0, lib)
text = "// " + (root / "spec.txt").read_text()
if lib or os.environ.get("PYTHONPATH"):
    import generated_from

    text += "// version %s\\n" % generated_from.VERSION
(root / "gen").mkdir(exist_ok=True)
(root / "gen" / "top.v").write_text(text)
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


def test_first_generation_is_serialized_across_processes_without_eager_run_root(tmp_path):
    world = World(tmp_path)
    world.design_file.with_name("gen.py").write_text(
        "import time\n"
        + GENERATOR.replace('(root / "gen").mkdir', 'time.sleep(0.3)\n(root / "gen").mkdir')
    )
    load = (
        "from pathlib import Path\n"
        "from xeda import Design\n"
        "from xeda.design import loading_in_run_root\n"
        "from xeda.run_root import ensure_run_root\n"
        f"design = Path({str(world.design_file)!r})\n"
        f"root = Path({str(world.run_root)!r})\n"
        "provider = lambda create: ensure_run_root(root, start=design.parent, create=create)\n"
        "with loading_in_run_root(provider):\n"
        "    Design.from_file(design)\n"
    )
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    children = [
        subprocess.Popen([sys.executable, "-c", load], cwd=world.root, env=env) for _ in range(2)
    ]
    codes = [child.wait(timeout=30) for child in children]
    assert codes == [0, 0]
    assert world.runs == 1
    assert world.run_root.is_dir(), "the successful generation must leave its record root"


def test_different_generator_identities_serialize_same_design_tree(tmp_path):
    world = World(tmp_path)
    events = tmp_path / "events.log"
    (world.root / "gen.py").write_text(
        "import os, sys, time\n"
        "from pathlib import Path\n"
        "root = Path(os.environ['DESIGN_ROOT'])\n"
        "with open(sys.argv[1], 'a') as f: f.write('start ' + sys.argv[2] + '\\n')\n"
        "time.sleep(0.3)\n"
        "(root / 'gen').mkdir(exist_ok=True)\n"
        "(root / 'gen' / 'top.v').write_text('// ' + sys.argv[2] + '\\n')\n"
        "with open(sys.argv[1], 'a') as f: f.write('end ' + sys.argv[2] + '\\n')\n"
    )
    design_files = []
    for tag in ("a", "b"):
        design = world.root / f"design-{tag}.yaml"
        design.write_text(
            "name: generated\n"
            "rtl:\n"
            "  sources: [gen/top.v]\n"
            "  top: top\n"
            "  generator:\n"
            f"    executable: {sys.executable!r}\n"
            f"    args: ['gen.py', {str(events)!r}, {tag!r}]\n"
            "    sources: [gen.py]\n"
        )
        design_files.append(design)
    load = (
        "from pathlib import Path\n"
        "from xeda import Design\n"
        "from xeda.design import loading_in_run_root\n"
        "from xeda.run_root import ensure_run_root\n"
        f"design = Path({str(design_files[0])!r}) if __import__('sys').argv[1] == 'a' else Path({str(design_files[1])!r})\n"
        f"root = Path({str(world.run_root)!r})\n"
        "provider = lambda create: ensure_run_root(root, start=design.parent, create=create)\n"
        "with loading_in_run_root(provider):\n"
        "    Design.from_file(design)\n"
    )
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    children = [
        subprocess.Popen([sys.executable, "-c", load, tag], cwd=world.root, env=env)
        for tag in ("a", "b")
    ]
    assert [child.wait(timeout=30) for child in children] == [0, 0]
    assert events.read_text().splitlines() in (
        ["start a", "end a", "start b", "end b"],
        ["start b", "end b", "start a", "end a"],
    )


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


def test_inputs_are_rechecked_after_waiting_for_the_record_lock(tmp_path, monkeypatch):
    import xeda.generation as generation

    world = World(tmp_path)
    world.load()
    spec = world.root / "spec.txt"
    spec.write_text("two\n")
    acquire = generation._locked
    changed = False

    from contextlib import contextmanager

    @contextmanager
    def change_after_acquire(entry):
        nonlocal changed
        with acquire(entry):
            if not changed:
                changed = True
                spec.write_text("three\n")
            yield

    monkeypatch.setattr(generation, "_locked", change_after_acquire)
    world.load()
    assert world.runs == 2
    assert world.generated.read_text() == "// three\n"
    world.load()
    assert world.runs == 2, "the new identity was not recorded after the lock wait"


@pytest.mark.skipif(os.name == "nt", reason="directory flock is POSIX-only")
def test_interrupted_design_root_lock_closes_its_directory_descriptor(tmp_path, monkeypatch):
    from xeda.flow_runner import run_lock

    opened = []
    open_directory = os.open

    def capture_open(path, flags):
        descriptor = open_directory(path, flags)
        opened.append(descriptor)
        return descriptor

    def interrupt(*_args):
        raise KeyboardInterrupt

    monkeypatch.setattr(run_lock.os, "open", capture_open)
    monkeypatch.setattr(run_lock.fcntl, "flock", interrupt)
    with pytest.raises(KeyboardInterrupt):
        with run_lock.generator_design_lock(tmp_path):
            pytest.fail("the interrupted lock was acquired")
    assert len(opened) == 1
    with pytest.raises(OSError):
        os.fstat(opened[0])


@pytest.mark.skipif(os.name == "nt", reason="directory flock is POSIX-only")
def test_design_root_lock_is_reentrant_for_nested_loads(tmp_path):
    from xeda.flow_runner.run_lock import generator_design_lock

    with generator_design_lock(tmp_path):
        with generator_design_lock(tmp_path):
            pass


@pytest.mark.skipif(os.name == "nt", reason="temporary executable scripts need POSIX execute bits")
def test_direct_executable_content_is_part_of_the_generator_identity(tmp_path):
    from xeda.design import Generator
    from xeda.generation import generation_identity

    root = tmp_path / "design"
    root.mkdir()
    source = root / "input.txt"
    source.write_text("input\n")
    executable = root / "generator-tool"
    executable.write_text("#!/bin/sh\nexit 0\n")
    executable.chmod(0o755)
    generator = Generator(executable=str(executable), sources=[source])
    before = generation_identity(generator, root)
    executable.write_text("#!/bin/sh\n# changed tool\nexit 0\n")
    executable.chmod(0o755)
    assert generation_identity(generator, root) != before


@pytest.mark.skipif(os.name == "nt", reason="temporary executable scripts need POSIX execute bits")
def test_execution_uses_the_same_tool_selected_from_generator_cwd_and_path(tmp_path):
    from xeda.design import Generator

    root = tmp_path / "design"
    child_cwd = root / "build"
    bin_dir = tmp_path / "custom-bin"
    child_cwd.mkdir(parents=True)
    bin_dir.mkdir()
    marker = tmp_path / "selected-tool-ran"
    executable = bin_dir / "generator-tool"
    executable.write_text('#!/bin/sh\nprintf ran > "$1"\n')
    executable.chmod(0o755)
    generator = Generator(
        executable="generator-tool",
        args=[str(marker)],
        cwd=str(child_cwd),
        env={"PATH": str(bin_dir)},
    )
    assert generator.execution_executable_path(root) == executable.resolve()
    generator.run()
    assert marker.read_text() == "ran"


@pytest.mark.skipif(os.name == "nt", reason="temporary executable scripts need POSIX execute bits")
def test_generator_execution_preserves_a_symlink_executable_alias_in_argv0(tmp_path):
    from xeda.design import Generator

    root = tmp_path / "design"
    root.mkdir()
    marker = tmp_path / "argv0"
    target = root / "real-tool"
    target.write_text('#!/bin/sh\nprintf "%s" "$0" > "$1"\n')
    target.chmod(0o755)
    alias = root / "tool-alias"
    alias.symlink_to(target)
    generator = Generator(executable=str(alias), args=[str(marker)])

    assert generator.execution_executable_path(root) == alias
    generator.run()
    assert marker.read_text() == str(alias)


def test_process_generation_resolves_relative_design_root_before_changing_directory(
    tmp_path, monkeypatch
):
    from xeda.design import Design

    (tmp_path / "design").mkdir()
    monkeypatch.chdir(tmp_path)
    Design.process_generation({"design_root": Path("design"), "rtl": {"generator": "true"}})


@pytest.mark.skipif(os.name == "nt", reason="temporary executable scripts need POSIX execute bits")
def test_chisel_identity_and_run_share_the_selected_mill_and_bloop_commands(tmp_path, monkeypatch):
    from xeda.design import ChiselGenerator
    from xeda.generation import generation_identity

    root = tmp_path / "design"
    root.mkdir()
    source = root / "build.sc"
    source.write_text("// build\n")
    mill = root / "mill"
    mill.write_text("#!/bin/sh\nexit 0\n")
    mill.chmod(0o755)
    mill_generator = ChiselGenerator(
        build_system="mill", project="core", cwd=str(root), sources=[source]
    )
    mill_command = mill_generator.execution_command(root)
    assert mill_command == ["./mill", "core.run"]
    assert mill_generator.execution_executable_path(root) == mill.resolve()
    ran = []
    monkeypatch.setattr(
        ChiselGenerator, "run_cmd", lambda self, command, **_kwargs: ran.append(command)
    )
    mill_generator.run_mill()
    assert ran == [mill_command]
    old_identity = generation_identity(mill_generator, root)
    mill.write_text("#!/bin/sh\n# changed\nexit 0\n")
    mill.chmod(0o755)
    assert generation_identity(mill_generator, root) != old_identity

    bin_dir = root / "bin"
    bin_dir.mkdir()
    bloop = bin_dir / "bloop"
    bloop.write_text("#!/bin/sh\nexit 0\n")
    bloop.chmod(0o755)
    bloop_generator = ChiselGenerator(
        build_system="bloop",
        project="core",
        cwd=str(root),
        env={"PATH": str(bin_dir)},
        sources=[source],
    )
    bloop_command = bloop_generator.execution_command(root)
    assert bloop_command == ["bloop", "run", "core"]
    assert bloop_generator.execution_executable_path(root) == bloop.resolve()
    ran.clear()
    bloop_generator.run_bloop()
    assert ran == [bloop_command]


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


# --- an input outside the design: a library tree named in `sources` ----------------------------


def _library(tmp_path: Path, body: str = "VERSION = 1\n") -> Path:
    """A Python package in a directory beside the design (an editable clone of litex, say), which
    the generator imports and the design names in `sources`."""
    package = tmp_path / "lib" / "generated_from"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text(body)
    return package


def _world_reading(tmp_path: Path, *extra: str) -> World:
    """A design whose generator imports the library beside it and names it in `sources`."""
    library = tmp_path / "lib"
    return World(
        tmp_path,
        args=["gen.py", str(tmp_path / "runs.log"), str(library), *extra],
        sources=["spec.txt", "gen.py", "../lib"],
    )


def test_a_change_to_a_library_the_generator_reads_runs_it_again(tmp_path):
    package = _library(tmp_path)
    world = _world_reading(tmp_path)
    world.load()
    assert world.runs == 1
    world.load()
    assert world.runs == 1, "an unchanged library is not a change"
    (package / "__init__.py").write_text("VERSION = 2\n")
    world.load()
    assert world.runs == 2, "a library the generator reads changed"
    assert world.generated.read_text() == "// one\n// version 2\n"


def test_a_symlinked_directory_in_a_library_is_followed(tmp_path):
    package = _library(tmp_path)
    external = tmp_path / "external_models"
    external.mkdir()
    model = external / "model.py"
    model.write_text("VALUE = 1\n")
    (package / "models").symlink_to(external, target_is_directory=True)
    world = _world_reading(tmp_path)
    world.load()
    model.write_text("VALUE = 2\n")
    world.load()
    assert world.runs == 2


def test_a_file_added_to_a_library_runs_the_generator_again(tmp_path):
    package = _library(tmp_path)
    world = _world_reading(tmp_path)
    world.load()
    (package / "platforms.py").write_text("BOARDS = ['arty']\n")
    world.load()
    assert world.runs == 2


def test_touching_a_library_does_not_run_the_generator_again(tmp_path):
    package = _library(tmp_path)
    world = _world_reading(tmp_path)
    world.load()
    _newest(package / "__init__.py")
    world.load()
    assert world.runs == 1


def test_a_generator_writing_bytecode_into_a_source_directory_records_on_its_second_run(tmp_path):
    """Whatever a generator writes into a directory it reads is part of what it read, and no
    language is special: Python's `__pycache__` changes the directory during the first run, so
    that run's inputs are not the ones it started with and keeps no record; the second finds the
    directory as the first left it, generates and records; the third is up to date."""
    package = _library(tmp_path)
    world = _world_reading(tmp_path, "bytecode")
    world.load()
    assert world.runs == 1
    assert (package / "__pycache__").is_dir(), "the generator was to leave bytecode there"
    assert world.records() == [], "the inputs changed while it ran: a record would be of other ones"
    world.load()
    assert world.runs == 2
    assert len(world.records()) == 1
    world.load()
    assert world.runs == 2


def test_a_library_an_environment_selects_is_what_sources_names_not_what_xeda_imports(tmp_path):
    """The generator runs in the environment the design gives it, not xeda's interpreter: a
    package it imports from there is judged by naming that tree in `sources`, and an edit to it
    regenerates, with the new version in the output."""
    package = tmp_path / "site_B" / "generated_from"
    package.mkdir(parents=True)
    (package / "__init__.py").write_text("VERSION = 1\n")
    world = World(
        tmp_path,
        args=["gen.py", str(tmp_path / "runs.log")],
        sources=["spec.txt", "gen.py", "../site_B/generated_from"],
        env={"PYTHONPATH": str(tmp_path / "site_B"), "PATH": os.environ["PATH"]},
    )
    world.load()
    assert world.generated.read_text() == "// one\n// version 1\n"
    world.load()
    assert world.runs == 1
    (package / "__init__.py").write_text("VERSION = 2\n")
    world.load()
    assert world.runs == 2, "an edit to the tree the generator imports from reused stale output"
    assert world.generated.read_text() == "// one\n// version 2\n"


def test_packages_is_not_a_generator_field(tmp_path):
    """A generator is an external tool: xeda looks up no installed package for it."""
    world = World(tmp_path, packages=["litex"])
    with pytest.raises(DesignValidationError, match="packages"):
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
    assert "`run_only_if_sources_modified` was removed: use `always_runs: true`" in str(error.value)
    assert world.runs == 0


def test_run_only_if_sources_modified_true_is_to_be_deleted_never_turned_into_always_runs(
    tmp_path,
):
    """The removed switch was on by default: a design that wrote `true` has the behavior content
    judging gives every generator, and `always_runs` would be the opposite of it."""
    world = World(tmp_path, run_only_if_sources_modified=True)
    with pytest.raises(DesignValidationError) as error:
        world.load()
    message = str(error.value)
    assert "`run_only_if_sources_modified` was removed: delete it" in message
    assert "use `always_runs" not in message
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


def test_generated_sources_naming_a_pattern_the_generator_fills_load_on_a_fresh_tree(tmp_path):
    """The check against `rtl.sources` is made where the tree is complete: before the generator
    runs, `gen/*.v` matches nothing, and a first load must not be refused for that."""
    world = World(tmp_path, generated_sources=["gen/top.v"])
    world.design_file.write_text(
        world.design_file.read_text().replace("[gen/top.v]", '["gen/*.v"]')
    )
    world.load()
    assert world.runs == 1
    world.load()
    assert world.runs == 1


def test_generated_sources_that_are_not_rtl_sources_are_refused_on_every_load(tmp_path):
    """`generated_sources` is *which of `rtl.sources`* the generator writes. One that is not among
    them leaves the real generated source unjudged, so a stale one would be reused silently."""
    world = World(tmp_path, generated_sources=["hand.v"])
    (world.root / "hand.v").write_text("// hand written\n")
    for expected_runs in (1, 1):
        with pytest.raises(DesignValidationError, match="not among `rtl.sources`: .*hand.v"):
            world.load()
        assert world.runs == expected_runs


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


def test_a_record_with_non_string_output_parts_runs_the_generator(tmp_path):
    world = World(tmp_path)
    world.load()
    [record] = world.records()
    data = yaml.safe_load(record.read_text())
    data["outputs"] = [[["not-a-name"], "not-a-digest"]]
    record.write_text(yaml.safe_dump(data))
    world.load()
    assert world.runs == 2
    data = yaml.safe_load(record.read_text())
    data["outputs"] *= 2
    record.write_text(yaml.safe_dump(data))
    world.load()
    assert world.runs == 3, "duplicate output names are a malformed record"


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


def _planned_world(tmp_path, monkeypatch) -> World:
    """A design whose generator has run once, so that a record says it is up to date, with the
    working directory and tools a launch needs."""
    from .tool_utils import use_fake_tools

    use_fake_tools(monkeypatch)
    monkeypatch.chdir(tmp_path)
    world = World(tmp_path)
    world.design_file.write_text(
        world.design_file.read_text().replace("  top: top\n", "  top: top\n  clock: {port: clk}\n")
    )
    world.load()
    assert world.runs == 1 and len(world.records()) == 1
    return world


def test_the_api_plans_a_design_whose_generation_is_up_to_date(tmp_path, monkeypatch):
    from xeda.flow_runner import DefaultRunner

    world = _planned_world(tmp_path, monkeypatch)
    plan = DefaultRunner(world.run_root, display_results=False).plan(
        "vivado_synth", world.design_file, flow_settings={"fpga": {"part": "xc7a35ticsg324-1L"}}
    )
    assert plan.requested == "vivado_synth"
    assert world.runs == 1


@pytest.mark.parametrize("option", ["rebuild_all", "clean"])
def test_the_api_refuses_to_plan_what_rebuild_all_would_generate(tmp_path, monkeypatch, option):
    """A plan describes the launch it stands for: `rebuild_all` (which `clean` implies) runs the
    generator whatever its record says, so the design needs one, and planning starts none."""
    from xeda.flow_runner import DefaultRunner

    world = _planned_world(tmp_path, monkeypatch)
    before = world.records()[0].read_bytes()
    with pytest.raises(DesignValidationError, match="Cannot plan a design that needs a generator"):
        DefaultRunner(world.run_root, display_results=False, **{option: True}).plan(
            "vivado_synth", world.design_file, flow_settings={"fpga": {"part": "xc7a35ticsg324-1L"}}
        )
    assert world.runs == 1
    assert world.records()[0].read_bytes() == before


@pytest.mark.parametrize("option", ["--rebuild-all", "--clean"])
def test_the_command_line_dry_run_follows_rebuild_all_like_the_launch(
    tmp_path, monkeypatch, option
):
    """`--dry-run` plans a fresh generation; with `--rebuild-all` (or `--clean`) the launch would
    generate, so it is refused with the same error a stale record gets -- as a JSON document under
    `--json` -- and no generator runs."""
    import json

    from click.testing import CliRunner

    from xeda.cli import cli

    world = _planned_world(tmp_path, monkeypatch)
    base = [
        "run", "vivado_synth", str(world.design_file), "--dry-run", "--json",
        "--run-root", str(world.run_root), "-s", "fpga.part=xc7a35ticsg324-1L",
    ]  # fmt: skip
    planned = CliRunner().invoke(cli, base)
    assert planned.exit_code == 0, planned.output
    assert json.loads(planned.stdout)["success"] is True
    refused = CliRunner().invoke(cli, [*base, option])
    assert refused.exit_code != 0
    document = json.loads(refused.stdout)
    assert document["success"] is False
    assert "Cannot plan a design that needs a generator" in document["error"]["message"]
    assert world.runs == 1


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


# --- a file named twice is one entry of the record --------------------------------------------


def _record_outputs(world: "World") -> list:
    (record,) = world.records()
    return yaml.safe_load(record.read_text())["outputs"]


def _declare_sources(world: "World", sources: str) -> None:
    world.design_file.write_text(
        world.design_file.read_text().replace("sources: [gen/top.v]", f"sources: {sources}")
    )


def test_a_source_named_by_a_pattern_and_by_its_path_is_recorded_once(tmp_path):
    """`gen/*.v` and `gen/top.v` expand to the same file. A record naming it twice reads back as
    damaged, so the generator that just wrote it would run again on every load."""
    world = World(tmp_path)
    _declare_sources(world, "['gen/*.v', 'gen/top.v']")
    world.load()
    assert world.runs == 1
    assert [name for name, _ in _record_outputs(world)] == ["gen/top.v"]
    world.load()
    world.load()
    assert world.runs == 1, "a record of overlapping spellings was judged malformed"
    world.generated.write_text("// edited by hand\n")
    world.load()
    assert world.runs == 2, "the one entry still vouches for the content"


def test_a_generated_directory_and_a_file_in_it_are_recorded_once(tmp_path):
    """A directory is recorded entry by entry, and a file it holds may be declared too."""
    world = World(tmp_path, generated_sources=["gen", "gen/top.v"])
    world.load()
    assert world.runs == 1
    names = [name for name, _ in _record_outputs(world)]
    assert names == ["gen", "gen/top.v"]
    world.load()
    assert world.runs == 1, "a record of a directory and its file was judged malformed"


def test_a_record_that_names_a_source_twice_is_still_damaged(tmp_path, caplog):
    """The check stays for a record some other hand wrote: it is not what the fix relies on."""
    world = World(tmp_path)
    world.load()
    (record,) = world.records()
    data = yaml.safe_load(record.read_text())
    data["outputs"] = data["outputs"] * 2
    record.write_text(yaml.safe_dump(data))
    with caplog.at_level("INFO", logger="xeda.design"):
        world.load()
    assert world.runs == 2
    assert "is malformed" in caplog.text
    assert len(_record_outputs(world)) == 1, "the new record names each source once"
    world.load()
    assert world.runs == 2


@pytest.mark.skipif(os.name == "nt", reason="symbolic links need privileges on Windows")
def test_digests_have_one_entry_per_name_whatever_order_the_paths_come_in(tmp_path):
    """A link given as itself is followed (its content is what a design declares) and, listed
    inside its directory, is recorded as a link: two digests for one name. The declared path's
    wins, whichever comes first, so collapsing equal pairs would not have been enough."""
    from xeda.digest import record_file
    from xeda.generation import _digests

    root = tmp_path.resolve()
    (root / "gen").mkdir()
    (root / "real.v").write_text("// real\n")
    link = root / "gen" / "ln.v"
    link.symlink_to(root / "real.v")
    (root / "gen" / "loop").symlink_to(root / "gen")  # a link to its own directory
    as_listed = record_file(link, follow_symlinks=False).sha
    as_declared = record_file(link).sha
    assert as_listed != as_declared

    forward = _digests([root / "gen", link], root)
    backward = _digests([link, root / "gen"], root)
    assert forward == backward
    names = [name for name, _ in forward]
    assert names == sorted(set(names)) == ["gen", "gen/ln.v", "gen/loop"]
    assert dict(forward)["gen/ln.v"] == as_declared
    assert _digests([root / "gen", root / "gen"], root) == _digests([root / "gen"], root)


# --- `args` is one stored value, whichever way the design spells it ---------------------------


def test_generator_args_written_as_text_are_words_and_as_a_list_stay_as_given():
    """A string `args` is split on whitespace exactly as `command` is, so the argv is the same
    whichever spelling a design used; a list is the words already. Both generator kinds read the
    one stored list."""
    from xeda.design import ChiselGenerator, Generator

    text = Generator(executable="python3", args="gen.py  out.log")
    words = Generator(executable="python3", args=["gen.py", "out.log"])
    assert text.args == words.args == ["gen.py", "out.log"]
    assert text.execution_command() == words.execution_command() == ["python3", "gen.py", "out.log"]
    assert Generator(executable="python3", args=["a b", "c"]).args == ["a b", "c"]
    assert Generator(executable="python3").execution_command() == ["python3"]
    assert Generator(executable="python3", args="").args == []

    mill = ChiselGenerator(build_system="mill", project="hw", args="--width  8")
    assert mill.execution_command(Path("."))[-2:] == ["--width", "8"]
    bloop = ChiselGenerator(build_system="bloop", project="hw", args=["--width", "8"])
    assert bloop.execution_command(Path("."))[-3:] == ["--", "--width", "8"]


def test_generator_args_survive_assignment_and_reload():
    """Validators run again on assignment and on every reload: the list a string became is the
    list it stays."""
    from xeda.design import Generator

    generator = Generator(executable="python3", args="gen.py out.log")
    generator.args = generator.args
    assert generator.args == ["gen.py", "out.log"]
    generator.args = "other.py x"
    assert generator.args == ["other.py", "x"]
    reloaded = Generator.model_validate(generator.model_dump(mode="json"))
    assert reloaded.args == generator.args


def test_a_generator_with_text_args_runs_its_words(tmp_path):
    """End to end, with the spelling a design file would use: the generator that ran is the one
    asked for, and a second load reuses its record."""
    world = World(tmp_path, args=f"gen.py {tmp_path / 'runs.log'}")
    world.load()
    assert world.runs == 1
    assert world.generated.is_file()
    world.load()
    assert world.runs == 1


# --- what a log line says about the generator ---------------------------------------------


def _generator_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if "enerator" in r.getMessage()]


def test_the_log_names_the_design_and_what_its_generator_runs(tmp_path, caplog):
    """Not the class name (`Generator`, which says nothing): the design and the command."""
    world = World(tmp_path)
    command = f"{sys.executable} gen.py {world.counter}"
    with caplog.at_level("INFO", logger="xeda.design"):
        world.load()
    (line,) = [m for m in _generator_lines(caplog) if m.startswith("Running")]
    assert line.startswith(f"Running the generator of design 'generated' ({command}): ")
    assert "'Generator'" not in caplog.text
    caplog.clear()
    with caplog.at_level("INFO", logger="xeda.design"):
        world.load()
    (line,) = _generator_lines(caplog)
    assert line.startswith(f"Not running the generator of design 'generated' ({command}): ")


def test_a_generator_with_a_command_is_named_by_it(tmp_path, caplog):
    world = World(
        tmp_path, executable=None, command=f"{sys.executable} gen.py {tmp_path / 'runs.log'}"
    )
    with caplog.at_level("INFO", logger="xeda.design"):
        world.load()
    assert f"({sys.executable} gen.py " in caplog.text


def test_the_record_failure_names_the_design_and_command(tmp_path, caplog):
    """The messages of `xeda.generation` name the generator as the design's loader does."""
    from xeda.design import Generator
    from xeda.generation import Generation

    generator = Generator(executable="python3", args=["hdmi_demo.py", "--build"])
    generation = Generation(
        "why",
        run_root=lambda create: None,
        outputs=lambda: [],
        generator=generator,
        design_root=tmp_path,
        description=generator.describe("hdmi_demo", tmp_path),
    )
    with caplog.at_level("DEBUG", logger="xeda.generation"):
        generation.produced()
    assert (
        "Keeping no record of the generator of design 'hdmi_demo' (python3 hdmi_demo.py --build)"
        in caplog.text
    )


def test_a_generator_without_an_executable_is_named_by_its_kind():
    from xeda.design import ChiselGenerator, Generator

    assert Generator().describe("d") == "the generator of design 'd' (Generator)"
    assert (
        ChiselGenerator(project="gcd", main="Main").describe(None)
        == "the generator (mill gcd.runMain Main)"
    )
