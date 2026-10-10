"""The one checked deletion (`RunDirectory`): inside a run directory xeda chose, anything; in the
directory of a flow built without a launcher, nothing. Everything runs in scratch copies
under `tmp_path`."""

import json
import logging
import os
from pathlib import Path
from typing import ClassVar, Optional

import pytest
from click.testing import CliRunner

from xeda import Design
from xeda.cli import cli
from xeda.dataclass import Field
from xeda.flow import Flow, registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.run_dir import RunDirectory, RunDirectoryError

pytestmark = pytest.mark.python_compat


SQRT = Path(__file__).parent.parent / "examples" / "vhdl" / "sqrt"


class _Probe(Flow):
    """Does nothing: what matters is where it would run."""

    results_description: ClassVar[dict[str, str]] = {}

    def run(self) -> None:
        pass


class _Reads(Flow):
    """Reads the constraints its setting names."""

    results_description: ClassVar[dict[str, str]] = {}

    class Settings(Flow.Settings):
        constraints: Optional[Path] = Field(None, description="The constraints it reads.")

    def run(self) -> None:
        pass


@pytest.fixture(autouse=True, scope="module")
def _unregister_probe():
    yield
    for cls in (_Probe, _Reads):
        for name in (cls.name, cls.__name__):
            registered_flows.pop(name, None)


def _files(root: Path) -> dict:
    """Every file under `root` and its content, by relative path."""
    return {
        str(p.relative_to(root)): p.read_bytes()
        for p in sorted(root.rglob("*"))
        if p.is_file() and not p.is_symlink()
    }


def _tree(root: Path) -> Path:
    (root / "sub").mkdir(parents=True)
    (root / "a.txt").write_text("a\n")
    (root / "sub" / "b.txt").write_text("b\n")
    return root


def test_xeda_s_run_directory_is_emptied_and_its_files_removed(tmp_path):
    run_dir = _tree(tmp_path / "xeda_run" / "d" / "flow")
    owned = RunDirectory.claimed(run_dir, tmp_path / "xeda_run")
    assert owned.run_root == (tmp_path / "xeda_run").resolve()
    assert owned.remove("sub") == [run_dir.resolve() / "sub"]
    assert not (run_dir / "sub").exists()
    owned.clear()
    assert run_dir.is_dir() and not list(run_dir.iterdir())


def test_a_directory_is_xeda_s_only_under_the_run_root_it_claimed(tmp_path):
    """A `managed` flag alone never authorizes clearing a path: it must lie under the run root."""
    elsewhere = _tree(tmp_path / "elsewhere")
    with pytest.raises(ValueError, match="not under the run root"):
        RunDirectory.claimed(elsewhere, tmp_path / "xeda_run")
    with pytest.raises(ValueError, match="not under the run root"):
        RunDirectory.claimed(tmp_path / "xeda_run", tmp_path / "xeda_run")  # the root itself
    assert _files(elsewhere) == {"a.txt": b"a\n", "sub/b.txt": b"b\n"}


def test_nothing_outside_the_run_directory_is_deleted_through_it(tmp_path):
    """Containment is decided without following a symbolic link out of the directory: a link is
    removed as itself, and a path through one that leads out is refused."""
    outside = _tree(tmp_path / "outside")
    run_dir = tmp_path / "xeda_run" / "d" / "flow"
    run_dir.mkdir(parents=True)
    (run_dir / "link").symlink_to(outside, target_is_directory=True)
    owned = RunDirectory.claimed(run_dir, tmp_path / "xeda_run")
    for escaping in ("link/a.txt", "link/sub", "../../../outside/a.txt", str(outside / "a.txt")):
        with pytest.raises(RunDirectoryError, match="not inside the run directory") as refused:
            owned.remove(escaping)
        if escaping.startswith("link/"):  # a link a tool may have made: named, as the cause
            assert f"{run_dir.resolve() / 'link'} is a symbolic link out of it" in str(
                refused.value
            )
    assert owned.remove("link") == [run_dir.resolve() / "link"]
    assert _files(outside) == {"a.txt": b"a\n", "sub/b.txt": b"b\n"}


def test_a_flow_constructed_without_a_launcher_deletes_nothing(tmp_path):
    """A flow gets its run directory from the launcher; one created directly holds an
    `unlaunched` one, in which xeda deletes nothing (each removal is logged, not done)."""

    class ProbePurge(Flow):
        """Purges its run directory."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self) -> None:
            self.purge_run_path()

    work = _tree(tmp_path / "work")
    try:
        flow = ProbePurge({}, Design(name="d", design_root=work, rtl={"sources": []}), work)
        flow.run()
    finally:
        for name in (ProbePurge.name, ProbePurge.__name__):
            registered_flows.pop(name, None)
    assert _files(work) == {"a.txt": b"a\n", "sub/b.txt": b"b\n"}


def test_a_flow_s_run_directory_is_decided_once(tmp_path):
    """The launcher hands a flow its `RunDirectory` when it constructs it; nothing replaces it
    afterwards, and one for another directory is refused."""

    class ProbeOwner(Flow):
        """Does nothing."""

        results_description: ClassVar[dict[str, str]] = {}

        def run(self) -> None:
            pass

    work = _tree(tmp_path / "work")
    design = Design(name="d", design_root=work, rtl={"sources": []})
    try:
        flow = ProbeOwner({}, design, work)
        assert flow.run_directory.run_root is None
        with pytest.raises(AttributeError):
            flow.run_directory = RunDirectory.claimed(work, tmp_path)  # type: ignore[misc]
        with pytest.raises(ValueError, match="is not the run directory"):
            ProbeOwner({}, design, work, run_directory=RunDirectory.unlaunched(tmp_path))
    finally:
        for name in (ProbeOwner.name, ProbeOwner.__name__):
            registered_flows.pop(name, None)


def test_every_launched_run_directory_is_xedas(tmp_path, monkeypatch):
    """A launched flow always runs in a directory the launcher chose under its run root."""
    work = _tree(tmp_path / "work")
    monkeypatch.chdir(work)
    design = Design(name="d", design_root=work, rtl={"sources": []})
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    flow = runner.run_flow(_Probe, design, {})
    assert flow.run_path == (tmp_path / "xeda_run" / "d" / _Probe.name).resolve()
    assert flow.run_directory.run_root == (tmp_path / "xeda_run").resolve()


# --- Every run directory lies inside its run root -----------------------------------------


@pytest.mark.parametrize("name", ["..", ".", "a b", "a/b", "1abc", "", "café"])
def test_a_design_name_that_names_no_directory_is_refused(tmp_path, name):
    from xeda.design import DesignValidationError

    with pytest.raises((ValueError, DesignValidationError), match="design name"):
        Design(name=name, design_root=tmp_path, rtl={"sources": []})


def test_a_design_named_dot_dot_deletes_nothing_beside_the_run_root(tmp_path, monkeypatch):
    """The run directory was `<start>/<flow>`, and `--clean` emptied it."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "verilator").mkdir()
    (tmp_path / "verilator" / "keep.txt").write_text("mine\n")
    (tmp_path / "d.v").write_text("module d; endmodule\n")
    (tmp_path / "d.toml").write_text('name = ".."\n[rtl]\nsources = ["d.v"]\ntop = "d"\n')
    result = CliRunner().invoke(cli, ["run", "verilator", "d.toml", "--clean", "--json"])
    assert json.loads(result.stdout)["success"] is False
    assert (tmp_path / "verilator" / "keep.txt").read_text() == "mine\n"


def test_a_run_directory_led_out_of_the_run_root_is_refused(tmp_path, monkeypatch):
    """`<root>/<design>` a link to elsewhere: refused before anything is written there, the lock
    file beside the run directory included."""
    elsewhere = _tree(tmp_path / "elsewhere")
    before = _files(elsewhere)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False, clean=True)
    run_root = runner.run_root
    (run_root / "d").symlink_to(elsewhere, target_is_directory=True)
    design = Design(name="d", design_root=tmp_path, rtl={"sources": []})
    with pytest.raises(RunDirectoryError, match="leads out of the run root"):
        runner.run_flow(_Probe, design, {})
    assert _files(elsewhere) == before
    assert not list(elsewhere.glob("*.lock"))


def test_delete_removes_the_links_it_is_given_first_and_never_follows_them(tmp_path):
    """A run directory reached by a name that is a link: the name goes with the directory, so none
    is left leading nowhere. A link is removed as itself, whatever it leads to by then."""
    root = tmp_path / "xeda_run"
    run_dir = _tree(root / "d" / "flow_cccccccccccccccc")
    link = root / "d" / "flow"
    link.symlink_to(run_dir, target_is_directory=True)
    other = _tree(root / "d" / "other")
    moved = root / "d" / "flow_moved"
    moved.symlink_to(other, target_is_directory=True)  # it leads elsewhere by now
    owned = RunDirectory.claimed(link, root)
    owned.delete(link, moved)
    assert not os.path.lexists(link) and not os.path.lexists(moved) and not run_dir.exists()
    assert _files(other) == {"a.txt": b"a\n", "sub/b.txt": b"b\n"}


def test_delete_leaves_a_name_that_is_no_link_or_is_gone_as_it_is(tmp_path):
    root = tmp_path / "xeda_run"
    run_dir = _tree(root / "d" / "flow")
    beside = _tree(root / "d" / "beside")
    owned = RunDirectory.claimed(run_dir, root)
    owned.delete(run_dir, beside, root / "d" / "gone")  # itself, a real directory, nothing
    assert not run_dir.exists()
    assert _files(beside) == {"a.txt": b"a\n", "sub/b.txt": b"b\n"}


def test_delete_refuses_a_link_that_lies_outside_the_run_root_and_removes_nothing(tmp_path):
    root = tmp_path / "xeda_run"
    run_dir = _tree(root / "d" / "flow")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "name").symlink_to(run_dir, target_is_directory=True)
    owned = RunDirectory.claimed(run_dir, root)
    with pytest.raises(RunDirectoryError, match="outside the run root"):
        owned.delete(outside / "name")
    assert (outside / "name").is_symlink() and _files(run_dir)


def test_a_design_file_in_the_flow_s_own_run_directory_is_refused(tmp_path):
    """Xeda empties and rewrites a run directory; a source kept there would be lost."""
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    own = runner.run_root / "d" / _Probe.name
    own.mkdir(parents=True)
    (own / "src.v").write_text("module d; endmodule\n")
    design = Design(name="d", design_root=own, rtl={"sources": ["src.v"], "top": "d"})
    with pytest.raises(RunDirectoryError, match="own run directory"):
        runner.run_flow(_Probe, design, {})
    assert (own / "src.v").read_text() == "module d; endmodule\n"


def test_a_file_a_setting_reads_in_the_flow_s_own_run_directory_is_refused(tmp_path):
    """The same for settings: refused before anything runs, the file kept."""
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    own = runner.run_root / "d" / _Reads.name
    own.mkdir(parents=True)
    (own / "c.xdc").write_text("period 5\n")
    design = Design(name="d", design_root=tmp_path, rtl={"sources": []})
    with pytest.raises(RunDirectoryError, match="own run directory"):
        runner.run_flow(_Reads, design, {"constraints": str(own / "c.xdc")})
    assert (own / "c.xdc").read_text() == "period 5\n"


# --- One kind of run directory -------------------------------------------------------------


def test_writable_replaces_a_link_as_itself_and_never_writes_through_it(tmp_path):
    """A link at the name xeda writes (left by a tool, or by hand) is removed as
    itself, so the write makes a regular file and the link's target is untouched."""
    (tmp_path / "mine.txt").write_text("mine\n")
    run_dir = tmp_path / "xeda_run" / "d" / "flow"
    run_dir.mkdir(parents=True)
    (run_dir / "script.tcl").symlink_to(tmp_path / "mine.txt")
    owned = RunDirectory.claimed(run_dir, tmp_path / "xeda_run")
    located = owned.writable("script.tcl")
    located.write_text("generated\n")
    assert not located.is_symlink() and located.read_text() == "generated\n"
    assert (tmp_path / "mine.txt").read_text() == "mine\n"
    with pytest.raises(RunDirectoryError, match="not inside the run directory"):
        owned.writable(tmp_path / "mine.txt")


def test_a_generated_file_goes_through_writable(tmp_path):
    """`copy_from_template`, the launcher's records and `Tool.run`'s `env.sh` never write through
    a link at their name."""
    (tmp_path / "mine.txt").write_text("mine\n")
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    # made and marked now, while empty: an unmarked directory holding files is refused
    run_root = runner.run_root
    run_dir = run_root / "d" / _Probe.name
    run_dir.mkdir(parents=True)
    for name in ("settings.json", "results.json"):
        (run_dir / name).symlink_to(tmp_path / "mine.txt")
    runner.run_flow(_Probe, Design(name="d", design_root=tmp_path, rtl={"sources": []}), {})
    assert (tmp_path / "mine.txt").read_text() == "mine\n"
    assert not (run_dir / "settings.json").is_symlink()


def test_an_unlaunched_directory_deletes_nothing(tmp_path, caplog):
    work = _tree(tmp_path / "work")
    unlaunched = RunDirectory.unlaunched(work)
    with caplog.at_level(logging.INFO, logger="xeda.run_dir"):
        assert unlaunched.remove("a.txt", "sub") == []
        unlaunched.clear()
    assert _files(work) == {"a.txt": b"a\n", "sub/b.txt": b"b\n"}
    assert "no run directory a launcher chose" in caplog.text


REMOVED_NAMES = [
    "OWNED_FILE",
    "read_ownership",
    "record_ownership",
    "require_replaceable",
    "require_writable",
    "given_setting",
    "setting_naming_directory",
    "claimed_files",
    "claimed_entries",
    "CWD_LOCK",
    "RunDirectory.users",
    ".managed",
    "replaces_project",
    "PROJECT_DIRECTORIES",
    "force_project",
    "overwrite_project",
    "force_icc2",
    ".xeda-owned",
    ".xeda.lock",
    "output_directories",
    "run_directory.discard(",
    "run_directory.removable(",
    "--cwd",
    "managed run directory",
]
#: where a removed name is still right: the hidden option that says it was removed
STILL_NAMED = {("cli.py", "--cwd")}


def test_nothing_of_the_user_s_directory_machinery_is_left():
    """No "user's directory" code path remains; a name of one found again is a leftover
    -- in code, a template, or a description `xeda list-settings` shows."""
    import xeda

    package = Path(xeda.__file__).parent
    found = sorted(
        f"{path.relative_to(package)}: {name}"
        for path in package.rglob("*")
        if path.is_file() and path.suffix in (".py", ".tcl", ".ys", ".sdc", ".xdc")
        for name in REMOVED_NAMES
        if name in path.read_text(errors="ignore")
        and (path.relative_to(package).as_posix(), name) not in STILL_NAMED
    )
    assert not found, found
