"""A design generator runs again when what it reads or produced changed -- judged by content.

The decision is `xeda.generation`'s: the key of a generation names a storage under the run root,
and the record in that storage holds the digest of every generated source that the last generation
left, and the digest of the environment that the generator inherited. No modification time decides
anything, so each of these tests would have the wrong answer under a mtime rule: a stale source
copied over a generated one with a newer mtime (`cp -p`) is caught, an edit given back its old
mtime is caught, and a plain `touch` of an input costs a hash rather than a re-run.

A generator writes into its storage, never into the design's tree. What Xeda keeps about it -- the
record -- is in the storage too; `test_isolation.py` holds the oracle for what a launch may write.
"""

import os
import shlex
import shutil
import subprocess
import sys
import time
import yaml
from pathlib import Path

import pytest

from xeda import Design
from xeda.design import DesignValidationError
from xeda.flow_runner.launch_generation import Generation, generate, generation_key
from xeda.generation import GenerationError, RECORD_NAME, generation_storage
from xeda.run_dir import RunDirectoryError

#: Writes `gen/top.v` from `spec.txt` into its storage, and appends a line to the file named by its
#: first argument (a run counter, outside the design tree). A second argument, when not empty, is a
#: directory it puts first on `sys.path` before importing `generated_from` from it, as litex's SoC
#: script imports litex (without one, `PYTHONPATH` in its environment does the same): the
#: package's version goes into the output. It writes no bytecode unless a third argument says
#: `bytecode`.
GENERATOR = """\
import os, sys
from pathlib import Path

root = Path(os.environ["DESIGN_ROOT"])
out = Path(os.environ["XEDA_GENERATED"])
sys.dont_write_bytecode = "bytecode" not in sys.argv[3:]
lib = sys.argv[2] if len(sys.argv) > 2 else ""
if lib:
    sys.path.insert(0, lib)
text = "// " + (root / "spec.txt").read_text()
if lib or os.environ.get("PYTHONPATH"):
    import generated_from

    text += "// version %s\\n" % generated_from.VERSION
(out / "gen").mkdir(exist_ok=True)
(out / "gen" / "top.v").write_text(text)
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
    """A design whose sources a generator writes, and a run root beside it."""

    def __init__(self, tmp_path: Path, **generator) -> None:
        self.root = tmp_path / "design"
        self.root.mkdir(exist_ok=True)
        self.counter = tmp_path / "runs.log"
        self.run_root = tmp_path / "xeda_run"
        (self.root / "gen.py").write_text(GENERATOR)
        (self.root / "spec.txt").write_text("one\n")
        generator = {
            "executable": sys.executable,
            "args": ["$DESIGN_ROOT/gen.py", str(self.counter)],
            "sources": ["spec.txt", "gen.py"],
            **generator,
        }
        self.design_file = self.root / "design.yaml"
        self.design_file.write_text(
            "name: generated\n"
            "rtl:\n"
            "  sources: [$XEDA_GENERATED/gen/top.v]\n"
            "  top: top\n"
            "  generator:\n"
            + "".join(f"    {key}: {_yaml(value)}\n" for key, value in generator.items())
        )

    @property
    def runs(self) -> int:
        """How many times the generator has run."""
        return len(self.counter.read_text().splitlines()) if self.counter.exists() else 0

    def load(self) -> Design:
        """Load the design: it runs nothing."""
        return Design.load(self.design_file)

    def generate(self, rebuild_all: bool = False, environ: dict | None = None) -> Generation:
        """Generate as a launch does, into the storage under the run root."""
        self.run_root.mkdir(exist_ok=True)
        generation = generate(
            self.load(),
            run_root=self.run_root,
            rebuild_all=rebuild_all,
            environ=dict(os.environ) if environ is None else environ,
        )
        assert generation is not None
        return generation

    @property
    def storage(self) -> Path:
        """The storage of the current configuration of the design."""
        key, _ = generation_key(self.load())
        return generation_storage(self.run_root.resolve(), "generated", key)

    @property
    def generated(self) -> Path:
        return self.storage / "gen" / "top.v"

    def records(self) -> list[Path]:
        """The records of every storage."""
        base = self.run_root / ".generated"
        return sorted(base.glob(f"*/*/{RECORD_NAME}")) if base.is_dir() else []


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
    generation = world.generate()
    assert world.runs == 1
    assert generation.matches == {"$XEDA_GENERATED/gen/top.v": ("gen/top.v",)}
    assert len(world.records()) == 1
    world.generate()
    world.generate()
    assert world.runs == 1, "a generation that need not run ran"


def test_first_generation_is_serialized_across_processes(tmp_path):
    world = World(tmp_path)
    (world.root / "gen.py").write_text(
        "import time\n"
        + GENERATOR.replace('(out / "gen").mkdir', 'time.sleep(0.3)\n(out / "gen").mkdir')
    )
    load = (
        "import os\n"
        "from pathlib import Path\n"
        "from xeda import Design\n"
        "from xeda.flow_runner.launch_generation import generate\n"
        f"design = Design.load(Path({str(world.design_file)!r}))\n"
        f"root = Path({str(world.run_root)!r})\n"
        "root.mkdir(exist_ok=True)\n"
        "generate(design, run_root=root.resolve(), environ=dict(os.environ))\n"
    )
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    children = [
        subprocess.Popen([sys.executable, "-c", load], cwd=world.root, env=env) for _ in range(2)
    ]
    codes = [child.wait(timeout=60) for child in children]
    assert codes == [0, 0]
    assert world.runs == 1
    assert len(world.records()) == 1


def test_a_stale_source_copied_over_a_generated_one_runs_the_generator(tmp_path):
    """`cp -p` of a stale output, which keeps its own (newer) modification time: a mtime rule
    sees the newest file of all in the right place and skips the generation it owes."""
    world = World(tmp_path)
    world.generate()
    stale = tmp_path / "stale.v"
    stale.write_text("// an older generation\n")
    _newest(stale)
    shutil.copy2(stale, world.generated)  # cp -p: the stale content with its newer mtime
    assert world.generated.stat().st_mtime > (world.root / "spec.txt").stat().st_mtime
    world.generate()
    assert world.runs == 2
    assert world.generated.read_text() == "// one\n"


def test_an_edit_given_back_its_old_mtime_runs_the_generator(tmp_path):
    """The inode change time moves whatever is done to the modification time, and the content
    decides anyway: an edited input is an input that changed."""
    world = World(tmp_path)
    world.generate()
    spec = world.root / "spec.txt"
    times = spec.stat()
    spec.write_text("two\n")
    _restore_times(spec, times)
    assert spec.stat().st_mtime == times.st_mtime
    world.generate()
    assert world.runs == 2
    assert world.generated.read_text() == "// two\n"


def test_touching_a_generator_source_does_not_run_it_again(tmp_path):
    """A `touch`, a `chmod`, a branch round-trip: the content is what is read, so this costs a
    hash, never a re-run. A mtime rule runs the generator again on every one of them."""
    world = World(tmp_path)
    world.generate()
    spec = world.root / "spec.txt"
    _newest(spec)
    assert spec.stat().st_mtime > world.generated.stat().st_mtime
    world.generate()
    assert world.runs == 1


def test_a_generated_source_that_was_deleted_runs_the_generator(tmp_path):
    world = World(tmp_path)
    world.generate()
    world.generated.unlink()
    world.generate()
    assert world.runs == 2


def test_a_changed_generator_configuration_is_another_storage(tmp_path):
    world = World(tmp_path)
    world.generate()
    world.design_file.write_text(
        world.design_file.read_text().replace("    args:", "    check: true\n    args:")
    )
    world.generate()
    assert world.runs == 1, "a setting written as its own default is the same configuration"
    world.design_file.write_text(world.design_file.read_text().replace("check: true", "cwd: '.'"))
    world.generate()
    assert world.runs == 2
    assert len(world.records()) == 2, "another configuration is another storage"


def test_inputs_are_rechecked_after_waiting_for_the_storage_lock(tmp_path, monkeypatch):
    """An input that changes while the launch waits for the lock names another configuration: the
    storage of the old key must not hold the output of the new input."""
    from contextlib import contextmanager

    from xeda.flow_runner import launch_generation

    world = World(tmp_path)
    spec = world.root / "spec.txt"
    spec.write_text("two\n")
    acquire = launch_generation.acquire_all
    changed = False

    @contextmanager
    def change_after_acquire(requests, **kwargs):
        nonlocal changed
        with acquire(requests, **kwargs) as locks:
            if not changed:
                changed = True
                spec.write_text("three\n")
            yield locks

    monkeypatch.setattr(launch_generation, "acquire_all", change_after_acquire)
    generation = world.generate()
    assert world.runs == 1
    assert (generation.storage / "gen" / "top.v").read_text() == "// three\n"
    assert generation.storage == world.storage, "the storage is that of the input it read"
    world.generate()
    assert world.runs == 1, "the new key was recorded"
    assert len(world.records()) == 1, "the old key left no storage"


@pytest.mark.skipif(os.name == "nt", reason="temporary executable scripts need POSIX execute bits")
def test_direct_executable_content_is_part_of_the_generation_key(tmp_path):
    world = World(tmp_path)
    tool = tmp_path / "generator-tool"
    tool.write_text("#!/bin/sh\nexit 0\n")
    tool.chmod(0o755)
    world.design_file.write_text(
        world.design_file.read_text().replace(repr(sys.executable), repr(str(tool)))
    )
    before = generation_key(world.load())[0]
    tool.write_text("#!/bin/sh\n# changed tool\nexit 0\n")
    tool.chmod(0o755)
    assert generation_key(world.load())[0] != before


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
        cwd="build",
        env={"PATH": str(bin_dir)},
    )
    assert generator.execution_executable_path(root) == executable.resolve()
    generator.run(root, tmp_path / "storage", {})
    assert marker.read_text() == "ran"


@pytest.mark.skipif(os.name == "nt", reason="temporary executable scripts need POSIX execute bits")
def test_generator_execution_preserves_a_symlink_executable_alias_in_argv0(tmp_path):
    from xeda.design import Generator

    root = tmp_path / "design"
    root.mkdir()
    storage = tmp_path / "storage"
    storage.mkdir()
    marker = tmp_path / "argv0"
    target = root / "real-tool"
    target.write_text('#!/bin/sh\nprintf "%s" "$0" > "$1"\n')
    target.chmod(0o755)
    alias = root / "tool-alias"
    alias.symlink_to(target)
    generator = Generator(executable=str(alias), args=[str(marker)])

    assert generator.execution_executable_path(root) == alias
    generator.run(root, storage, {})
    assert marker.read_text() == str(alias)


@pytest.mark.skipif(os.name == "nt", reason="temporary executable scripts need POSIX execute bits")
def test_chisel_identity_and_run_share_the_selected_mill_and_bloop_commands(tmp_path, monkeypatch):
    from xeda.design import ChiselGenerator

    root = tmp_path / "design"
    root.mkdir()
    storage = tmp_path / "storage"
    source = root / "build.sc"
    source.write_text("// build\n")
    mill = root / "mill"
    mill.write_text("#!/bin/sh\nexit 0\n")
    mill.chmod(0o755)
    mill_generator = ChiselGenerator(build_system="mill", project="core", cwd=".", sources=[source])
    mill_command = mill_generator.execution_command(root)
    assert mill_command == ["./mill", "core.run"]
    assert mill_generator.execution_executable_path(root) == mill.resolve()
    ran = []
    monkeypatch.setattr(
        ChiselGenerator, "run_cmd", lambda self, command, **_kwargs: ran.append(command)
    )
    mill_generator.run_mill(root, storage, {})
    assert ran == [mill_command]

    bin_dir = root / "bin"
    bin_dir.mkdir()
    bloop = bin_dir / "bloop"
    bloop.write_text("#!/bin/sh\nexit 0\n")
    bloop.chmod(0o755)
    bloop_generator = ChiselGenerator(
        build_system="bloop",
        project="core",
        cwd=".",
        env={"PATH": str(bin_dir)},
        sources=[source],
    )
    bloop_command = bloop_generator.execution_command(root)
    assert bloop_command == ["bloop", "run", "core"]
    assert bloop_generator.execution_executable_path(root) == bloop.resolve()
    ran.clear()
    bloop_generator.run_bloop(root, storage, {})
    assert ran == [bloop_command]


def test_bloop_project_discovery_ignores_empty_whitespace_entries(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from xeda.design import ChiselGenerator

    calls = []

    def run_cmd(self, command, **kwargs):
        calls.append(command)
        if command == ["bloop", "projects"]:
            return SimpleNamespace(stdout=b"\n  core\nother \t\n")
        return command

    monkeypatch.setattr(ChiselGenerator, "run_cmd", run_cmd)

    generator = ChiselGenerator(build_system="bloop")
    generator.run_bloop(tmp_path, tmp_path, {})
    assert generator.project == "core"
    assert calls == [["bloop", "projects"], ["bloop", "run", "core"]]


def test_bloop_project_discovery_reports_empty_output(monkeypatch, tmp_path):
    from types import SimpleNamespace

    from xeda.design import ChiselGenerator

    monkeypatch.setattr(
        ChiselGenerator,
        "run_cmd",
        lambda self, command, **kwargs: SimpleNamespace(stdout=b" \n\t "),
    )

    with pytest.raises(ValueError, match="No projects found"):
        ChiselGenerator(build_system="bloop").run_bloop(tmp_path, tmp_path, {})


def test_bloop_rejects_an_empty_project(monkeypatch, tmp_path):
    from xeda.design import ChiselGenerator

    monkeypatch.setattr(ChiselGenerator, "run_cmd", lambda self, command, **kwargs: command)

    with pytest.raises(ValueError, match="`project` must be specified"):
        ChiselGenerator(build_system="bloop", project="").run_bloop(tmp_path, tmp_path, {})


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
        args=["$DESIGN_ROOT/gen.py", str(tmp_path / "runs.log"), str(library), *extra],
        sources=["spec.txt", "gen.py", "../lib"],
    )


def test_a_change_to_a_library_the_generator_reads_runs_it_again(tmp_path):
    package = _library(tmp_path)
    world = _world_reading(tmp_path)
    world.generate()
    assert world.runs == 1
    world.generate()
    assert world.runs == 1, "an unchanged library is not a change"
    (package / "__init__.py").write_text("VERSION = 2\n")
    world.generate()
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
    world.generate()
    model.write_text("VALUE = 2\n")
    world.generate()
    assert world.runs == 2


def test_a_file_added_to_a_library_runs_the_generator_again(tmp_path):
    package = _library(tmp_path)
    world = _world_reading(tmp_path)
    world.generate()
    (package / "platforms.py").write_text("BOARDS = ['arty']\n")
    world.generate()
    assert world.runs == 2


def test_touching_a_library_does_not_run_the_generator_again(tmp_path):
    package = _library(tmp_path)
    world = _world_reading(tmp_path)
    world.generate()
    _newest(package / "__init__.py")
    world.generate()
    assert world.runs == 1


def test_a_generator_writing_bytecode_into_a_source_directory_records_on_its_second_run(tmp_path):
    """Whatever a generator writes into a directory it reads is part of what it read, and no
    language is special: Python's `__pycache__` changes the directory during the first run, so
    that run's inputs are not the ones it started with and its output is not vouched for; the
    second finds the directory as the first left it, generates and records; the third is up to
    date."""
    package = _library(tmp_path)
    world = _world_reading(tmp_path, "bytecode")
    world.generate()
    assert world.runs == 1
    assert (package / "__pycache__").is_dir(), "the generator was to leave bytecode there"
    assert world.records() == [], "the inputs changed while it ran: a record would be of other ones"
    stale = [p for p in (world.run_root / ".generated").glob("*/*") if p.is_dir()]
    assert stale == [], "the storage of the old key is removed: nothing vouches for it"
    world.generate()
    assert world.runs == 2
    assert len(world.records()) == 1
    world.generate()
    assert world.runs == 2


def test_a_launch_generates_again_until_nothing_changes_while_it_reads(tmp_path):
    """The lease of a launch is of a storage that its record vouches for: when the first run
    changed its own inputs, the launch runs the generator once more and then holds the storage."""
    from xeda.flow_runner.launch_generation import generated

    _library(tmp_path)
    world = _world_reading(tmp_path, "bytecode")
    world.run_root.mkdir()
    with generated(world.load(), run_root=world.run_root.resolve(), environ=dict(os.environ)) as g:
        assert g is not None and (g.by_owner[None].storage / RECORD_NAME).is_file()
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
        args=["$DESIGN_ROOT/gen.py", str(tmp_path / "runs.log")],
        sources=["spec.txt", "gen.py", "../site_B/generated_from"],
        env={"PYTHONPATH": str(tmp_path / "site_B"), "PATH": os.environ["PATH"]},
    )
    world.generate()
    assert world.generated.read_text() == "// one\n// version 1\n"
    world.generate()
    assert world.runs == 1
    (package / "__init__.py").write_text("VERSION = 2\n")
    world.generate()
    assert world.runs == 2, "an edit to the tree the generator imports from reused stale output"
    assert world.generated.read_text() == "// one\n// version 2\n"


def test_packages_is_not_a_generator_field(tmp_path):
    """A generator is an external tool: xeda looks up no installed package for it."""
    world = World(tmp_path, packages=["litex"])
    with pytest.raises(DesignValidationError, match="packages"):
        world.load()
    assert world.runs == 0


# --- what cannot be judged runs, as everywhere else in xeda ---------------------------------


def test_a_generator_that_declares_always_runs_runs_in_every_launch(tmp_path):
    world = World(tmp_path, always_runs=True)
    world.generate()
    world.generate()
    assert world.runs == 2


def test_a_generator_with_nothing_to_judge_it_by_runs_in_every_launch(tmp_path):
    world = World(tmp_path, sources=[])
    world.generate()
    world.generate()
    assert world.runs == 2


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


# --- only what the generator produces is judged ----------------------------------------------


def test_only_the_sources_the_generator_declares_it_generates_are_judged(tmp_path):
    """A design whose `rtl.sources` hold hand-written files too names what the generator writes
    in `generated_sources`; editing one of its own sources is then not a reason to generate."""
    world = World(tmp_path, generated_sources=["$XEDA_GENERATED/gen/top.v"])
    (world.root / "mine.v").write_text("// mine\n")
    world.design_file.write_text(
        world.design_file.read_text().replace(
            "[$XEDA_GENERATED/gen/top.v]", "[$XEDA_GENERATED/gen/top.v, mine.v]"
        )
    )
    world.generate()
    assert world.runs == 1
    (world.root / "mine.v").write_text("// mine, edited\n")
    world.generate()
    assert world.runs == 1
    world.generated.write_text("// not what it left\n")
    world.generate()
    assert world.runs == 2


def test_a_pattern_the_generator_fills_is_judged_by_what_it_matches(tmp_path):
    world = World(tmp_path)
    world.design_file.write_text(
        world.design_file.read_text().replace(
            "[$XEDA_GENERATED/gen/top.v]", "['$XEDA_GENERATED/gen/*.v']"
        )
    )
    world.generate()
    assert world.runs == 1
    world.generate()
    assert world.runs == 1
    (world.storage / "gen" / "extra.v").write_text("// another file the pattern matches\n")
    generation = world.generate()
    assert world.runs == 2, "the pattern names another set of sources than the record holds"
    assert generation.matches == {"$XEDA_GENERATED/gen/*.v": ("gen/top.v",)}


# --- the record itself ------------------------------------------------------------------------


def test_the_record_lies_in_the_storage_and_nothing_is_written_beside_the_design(tmp_path):
    world = World(tmp_path)
    world.generate()
    [record] = world.records()
    assert record.parent == world.storage
    assert sorted(p.name for p in world.root.rglob("*")) == ["design.yaml", "gen.py", "spec.txt"]


def test_a_record_of_another_format_or_a_damaged_one_runs_the_generator(tmp_path):
    world = World(tmp_path)
    world.generate()
    [record] = world.records()
    record.write_text("format: 999\n")
    world.generate()
    assert world.runs == 2
    record.write_text(": not yaml :\n[")
    world.generate()
    assert world.runs == 3


def test_a_record_with_non_string_output_parts_runs_the_generator(tmp_path):
    world = World(tmp_path)
    world.generate()
    [record] = world.records()
    data = yaml.safe_load(record.read_text())
    data["outputs"] = [[["not-a-name"], "not-a-digest"]]
    record.write_text(yaml.safe_dump(data))
    world.generate()
    assert world.runs == 2
    data = yaml.safe_load(record.read_text())
    data["outputs"] *= 2
    record.write_text(yaml.safe_dump(data))
    world.generate()
    assert world.runs == 3, "duplicate output names are a malformed record"


def test_a_record_of_another_configuration_runs_the_generator(tmp_path):
    world = World(tmp_path)
    world.generate()
    [record] = world.records()
    data = yaml.safe_load(record.read_text())
    data["key"] = "0" * 16
    record.write_text(yaml.safe_dump(data))
    world.generate()
    assert world.runs == 2


def test_the_storage_is_never_reached_through_a_link(tmp_path):
    world = World(tmp_path)
    world.generate()
    base = world.run_root / ".generated"
    elsewhere = world.run_root / "elsewhere"
    elsewhere.mkdir()
    moved = base.rename(world.run_root / "moved")
    base.symlink_to(elsewhere)
    # Not a design error: the run root is xeda's own, and the message says what to do about it.
    with pytest.raises(RunDirectoryError, match="symbolic link where Xeda keeps a cache"):
        world.generate()
    assert moved.is_dir()


# --- a directory a generator reads is judged by what is in it ---------------------------------


def test_a_directory_the_generator_reads_is_judged_by_its_files(tmp_path):
    """A source may name a directory -- a Chisel `src/main/scala`, a directory of templates. Its
    own digest is a constant, so it is expanded entry by entry, as the trace does for a directory
    a setting names: an edit inside it, or a file added to it, runs the generator again."""
    world = World(tmp_path, sources=["spec.txt", "gen.py", "templates"])
    templates = world.root / "templates"
    templates.mkdir()
    (templates / "top.v.in").write_text("// a template\n")
    world.generate()
    assert world.runs == 1
    world.generate()
    assert world.runs == 1
    (templates / "top.v.in").write_text("// an edited template\n")
    world.generate()
    assert world.runs == 2
    (templates / "another.v.in").write_text("// one more\n")
    world.generate()
    assert world.runs == 3
    _newest(templates / "another.v.in")
    world.generate()
    assert world.runs == 3, "a touch inside the directory costs a hash, not a run"


def test_a_generator_source_that_vanished_is_named(tmp_path):
    """Between the design's validation and the hash, a source can go: its own name is the error,
    not an errno from inside a digest."""
    world = World(tmp_path)
    design = world.load()
    (world.root / "spec.txt").unlink()
    world.run_root.mkdir()
    with pytest.raises(GenerationError, match="spec.txt"):
        generate(design, run_root=world.run_root.resolve(), environ={})


# --- a failed generation leaves nothing --------------------------------------------------------


def test_a_generator_that_fails_leaves_no_storage(tmp_path):
    world = World(tmp_path)
    (world.root / "fail").write_text("")
    with pytest.raises(GenerationError, match="exit"):
        world.generate()
    assert world.runs == 1
    assert [p for p in (world.run_root / ".generated").glob("*/*") if p.is_dir()] == []
    (world.root / "fail").unlink()
    world.generate()
    assert world.runs == 2 and len(world.records()) == 1


# --- the real entry point ---------------------------------------------------------------------


def test_rebuild_all_runs_the_generator_and_records_what_it_leaves(tmp_path):
    """`--rebuild-all` (and `--clean`, which implies it) is what forces everything to run again,
    a generation included -- there is no `touch` to fall back on. The record is still written, so
    the next ordinary launch is up to date."""
    world = World(tmp_path)
    world.generate()
    assert world.runs == 1
    world.generate()
    assert world.runs == 1
    world.generate(rebuild_all=True)
    assert world.runs == 2
    world.generate()
    assert world.runs == 2, "the record of the last generation is there again"


def test_a_dangling_link_in_a_directory_source_is_a_file_like_any_other(tmp_path):
    """An editor's lock file (`.#Top.scala`) is a link to nothing, and it is there exactly while
    the file is open: a design must still load. An entry of a directory is recorded as itself,
    as the trace records a listing, so a link is its target text and never followed."""
    world = World(tmp_path, sources=["spec.txt", "gen.py", "templates"])
    templates = world.root / "templates"
    templates.mkdir()
    (templates / "top.v.in").write_text("// a template\n")
    (templates / ".#top.v.in").symlink_to("nowhere/at/all")
    world.generate()
    assert world.runs == 1
    world.generate()
    assert world.runs == 1
    (templates / ".#top.v.in").unlink()
    (templates / ".#top.v.in").symlink_to("somewhere/else")
    world.generate()
    assert world.runs == 2, "the link names something else, which is a change like any other"


# --- a file named twice is one entry of the record --------------------------------------------


def _record_outputs(world: "World") -> list:
    (record,) = world.records()
    return yaml.safe_load(record.read_text())["outputs"]


def test_a_source_named_by_a_pattern_and_by_its_path_is_recorded_once(tmp_path):
    """`gen/*.v` and `gen/top.v` match the same file. A record naming it twice reads back as
    damaged, so the generator that just wrote it would run again on every launch."""
    world = World(tmp_path)
    world.design_file.write_text(
        world.design_file.read_text().replace(
            "[$XEDA_GENERATED/gen/top.v]",
            "['$XEDA_GENERATED/gen/*.v', '$XEDA_GENERATED/gen/top.v']",
        )
    )
    world.generate()
    assert world.runs == 1
    assert [name for name, _ in _record_outputs(world)] == ["gen/top.v"]
    world.generate()
    world.generate()
    assert world.runs == 1, "a record of overlapping spellings was judged malformed"
    world.generated.write_text("// edited by hand\n")
    world.generate()
    assert world.runs == 2, "the one entry still vouches for the content"


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
    asked for, and a second generation reuses its storage."""
    world = World(tmp_path, args=f"$DESIGN_ROOT/gen.py {tmp_path / 'runs.log'}")
    world.generate()
    assert world.runs == 1
    assert world.generated.is_file()
    world.generate()
    assert world.runs == 1


# --- what a log line says about the generator ---------------------------------------------


def _generator_lines(caplog) -> list[str]:
    """The messages of the generation stage that say whether the generator runs. A line of
    another logger never counts, whatever its text holds (a path may contain "generator")."""
    return [
        r.getMessage()
        for r in caplog.records
        if r.name == "xeda.flow_runner.launch_generation"
        and r.getMessage().startswith(("Running the generator", "Not running the generator"))
    ]


def test_the_log_names_the_design_and_what_its_generator_runs(tmp_path, caplog):
    """Not the class name (`Generator`, which says nothing): the design and the command."""
    world = World(tmp_path)
    command = shlex.join([sys.executable, "$DESIGN_ROOT/gen.py", str(world.counter)])
    with caplog.at_level("INFO", logger="xeda.flow_runner.launch_generation"):
        world.generate()
    (line,) = [m for m in _generator_lines(caplog) if m.startswith("Running")]
    assert line.startswith(f"Running the generator of design 'generated' ({command}): ")
    assert "'Generator'" not in caplog.text
    caplog.clear()
    with caplog.at_level("INFO", logger="xeda.flow_runner.launch_generation"):
        world.generate()
    (line,) = _generator_lines(caplog)
    assert line.startswith(f"Not running the generator of design 'generated' ({command}): ")


def test_a_generator_with_a_command_is_named_by_it(tmp_path, caplog):
    world = World(
        tmp_path,
        executable=None,
        args=[],
        command=f"{sys.executable} $DESIGN_ROOT/gen.py {tmp_path / 'runs.log'}",
    )
    world.design_file.write_text(
        "\n".join(
            line
            for line in world.design_file.read_text().splitlines()
            if "executable: None" not in line and "args: []" not in line
        )
        + "\n"
    )
    with caplog.at_level("INFO", logger="xeda.flow_runner.launch_generation"):
        world.generate()
    assert f"({sys.executable} '$DESIGN_ROOT/gen.py' " in caplog.text


def test_a_generator_without_an_executable_is_named_by_its_kind():
    from xeda.design import ChiselGenerator, Generator

    assert Generator().describe("d") == "the generator of design 'd' (Generator)"
    assert (
        ChiselGenerator(project="gcd", main="Main").describe(None)
        == "the generator (mill gcd.runMain Main)"
    )


def test_bloop_without_a_project_is_named_by_its_kind():
    from xeda.design import ChiselGenerator

    generator = ChiselGenerator(build_system="bloop")

    assert generator.execution_command() == ["bloop", "projects"]
    assert generator.command_text() == "ChiselGenerator"
    assert generator.describe("d") == "the generator of design 'd' (ChiselGenerator)"
