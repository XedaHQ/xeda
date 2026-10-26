"""Git dependencies are fetched at planning, into storage in the run root.

A local bare repository, made under `tmp_path`, serves as the remote through a `file://` URL.
A `git` shim on `PATH` records every Git command that Xeda starts, so a test can say that a call
started none.
"""

import ast
import json
import logging
import os
import shutil
import stat
import subprocess
import threading
from pathlib import Path
from typing import ClassVar

import pytest
import yaml
from pydantic import ValidationError

import xeda.design
import xeda.flow_runner.dependencies
from xeda import Design
from xeda.design import (
    DesignReference,
    DesignValidationError,
    GitReference,
    redacted_url,
)
from xeda.errors import DesignError
from xeda.design import SourceType
from xeda.flow import Flow, In, MetricSpec, registered_flows
from xeda.flow_runner import DefaultRunner
from xeda.flow_runner.dependencies import (
    DEPENDENCIES_DIRECTORY,
    dependency_key,
    dependency_storage,
    fetch,
    resolve_reference,
)
from xeda.records import read_record

from .flow_registry import preserved_flow_registry
from .tool_utils import require_git

SECRET = "s3cr3t-sentinel"
XILINX = {"fpga": {"part": "xc7a12tcsg325-1"}, "clocks": {"main_clock": {"period": "10 ns"}}}


#: The real Git, found before a test puts the shim on `PATH`: the commands of the test itself.
REAL_GIT = shutil.which("git") or "git"


def _git(*arguments: str, cwd: Path) -> str:
    return subprocess.run(
        [REAL_GIT, "-c", "user.name=t", "-c", "user.email=t@example.com", *arguments],
        cwd=cwd,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


class Remote:
    """A repository `lib` with a design file `dep.yaml`, served by a bare repository."""

    def __init__(self, root: Path) -> None:
        self.work = root / "work"
        self.bare = root / "remote.git"
        self.work.mkdir()
        _git("init", "--quiet", "--initial-branch=main", cwd=self.work)
        (self.work / "src").mkdir()
        (self.work / "src" / "a.v").write_text("module a; endmodule\n")
        (self.work / "dep.yaml").write_text(
            yaml.safe_dump(
                {
                    "name": "dep",
                    "rtl": {
                        "sources": ["src/a.v"],
                        "top": "dep_top",
                        "parameters": {"W": 8},
                        "clocks": ["clk"],
                    },
                }
            )
        )
        self.first = self.commit("first")
        _git("clone", "--quiet", "--bare", str(self.work), str(self.bare), cwd=root)
        _git("remote", "add", "origin", str(self.bare), cwd=self.work)

    def commit(self, message: str) -> str:
        _git("add", "-A", cwd=self.work)
        _git("commit", "--quiet", "-m", message, cwd=self.work)
        return _git("rev-parse", "HEAD", cwd=self.work)

    def advance(self) -> str:
        """Add a commit to the branch `main` of the bare repository, and return it."""
        (self.work / "src" / "a.v").write_text("module a; wire x; endmodule\n")
        commit = self.commit("second")
        _git("push", "--quiet", "origin", "main", cwd=self.work)
        return commit

    @property
    def url(self) -> str:
        return self.bare.as_uri()

    def moved_away(self) -> Path:
        """Move the bare repository away: the remote is unreachable."""
        away = self.bare.with_name("gone")
        self.bare.rename(away)
        return away


@pytest.fixture
def git_log(tmp_path, monkeypatch):
    """Record each Git command that Xeda starts, and keep the user's Git settings out."""
    require_git()
    real = REAL_GIT
    shim = tmp_path / "shim"
    shim.mkdir()
    log = tmp_path / "git-commands.log"
    program = shim / "git"
    program.write_text(f'#!/bin/sh\necho "$@" >> "{log}"\nexec "{real}" "$@"\n')
    program.chmod(program.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{shim}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    return lambda: log.read_text().splitlines() if log.exists() else []


@pytest.fixture
def remote(tmp_path, git_log) -> Remote:
    root = tmp_path / "remote"
    root.mkdir()
    return Remote(root)


def _design_file(
    root: Path, dependency: dict | str, *, extra: dict | None = None, name: str = "d.yaml"
) -> Path:
    """A design with one source of its own and the dependency, which supplies the rest."""
    (root / "top.v").write_text("module top; endmodule\n")
    spec = {
        "name": "d",
        "rtl": {"sources": ["top.v"], "clocks": ["clk"]},
        "dependencies": [dependency],
        **(extra or {}),
    }
    (root / name).write_text(yaml.safe_dump(spec))
    return root / name


def _pinned(remote: Remote, commit: str | None = None) -> dict:
    return {"uri": f"git+{remote.url}?commit={commit or remote.first}#dep.yaml", "rtl": {"pos": -1}}


def _runner(root: Path) -> DefaultRunner:
    return DefaultRunner(root / "xeda_run", display_results=False)


def _plan(root: Path, design_file: Path):
    return _runner(root).plan("yosys_fpga", str(design_file), flow_settings=dict(XILINX))


def _configuration(plan) -> dict:
    from xeda.frozen import thaw

    return thaw(plan.node(plan.requested).configuration)


def _tree(root: Path) -> set[Path]:
    return {p for p in root.rglob("*")} if root.exists() else set()


# ------------------------------------------------------------------ what planning does


def test_a_dependency_supplies_sources_and_metadata_at_planning(tmp_path, remote):
    plan = _plan(tmp_path, _design_file(tmp_path, _pinned(remote)))
    rtl = _configuration(plan)["rtl"]
    assert rtl["top"] == "dep_top" and rtl["parameters"] == {"W": 8}
    assert {"dependency": 0, "path": "src/a.v", "type": "Verilog"} in rtl["sources"]
    assert {"path": "top.v", "type": "Verilog"} in rtl["sources"]


def test_loading_reads_no_dependency_and_writes_nothing(tmp_path, remote, git_log):
    design = Design.from_file(_design_file(tmp_path, _pinned(remote)))
    assert git_log() == [] and not (tmp_path / "xeda_run").exists()
    assert [s.path.name for s in design.rtl.sources] == ["top.v"]
    assert design.rtl.top is None and len(design.dependencies) == 1


def test_planning_writes_only_dependency_storage(tmp_path, remote):
    design_file = _design_file(tmp_path, _pinned(remote))
    before = _tree(tmp_path)
    _plan(tmp_path, design_file)
    root = tmp_path / "xeda_run"
    storage_root = root / DEPENDENCIES_DIRECTORY
    for path in _tree(tmp_path) - before - {tmp_path / "git-commands.log"}:
        # the run root, its marker, and the dependency storage with its lock file
        assert path in (root, storage_root) or path.parent == root or storage_root in path.parents
    storage = root / DEPENDENCIES_DIRECTORY / dependency_key(remote.url, remote.first)
    assert (storage / "dep.yaml").is_file() and (storage / "dependency.yaml").is_file()
    assert not (storage / ".git").exists()
    assert [p.name for p in (root / DEPENDENCIES_DIRECTORY).iterdir() if "partial" in p.name] == []
    record = yaml.safe_load((storage / "dependency.yaml").read_text())
    assert record["commit"] == remote.first and record["repository"] == remote.url


def test_a_design_without_a_git_dependency_plans_with_no_write(tmp_path):
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    (tmp_path / "d.yaml").write_text(
        yaml.safe_dump({"name": "d", "rtl": {"sources": ["top.v"], "clocks": ["clk"]}})
    )
    _plan(tmp_path, tmp_path / "d.yaml")
    assert not (tmp_path / "xeda_run").exists()


def test_a_local_dependency_is_read_at_planning(tmp_path):
    lib = tmp_path / "lib"
    lib.mkdir()
    (lib / "b.v").write_text("module b; endmodule\n")
    (lib / "lib.yaml").write_text(
        yaml.safe_dump({"name": "lib", "rtl": {"sources": ["b.v"], "top": "b"}})
    )
    dependency = {"uri": "lib/lib.yaml", "rtl": {"pos": -1}}
    plan = _plan(tmp_path, _design_file(tmp_path, dependency))
    rtl = _configuration(plan)["rtl"]
    assert rtl["top"] == "b"
    assert not (tmp_path / "xeda_run").exists()


def test_an_invalid_spec_fetches_nothing(tmp_path, remote, git_log):
    design_file = _design_file(tmp_path, _pinned(remote))
    with pytest.raises(Exception, match="no_such_setting"):
        _runner(tmp_path).plan(
            "yosys_fpga", str(design_file), flow_settings={**XILINX, "no_such_setting": 1}
        )
    assert git_log() == [] and not (tmp_path / "xeda_run" / DEPENDENCIES_DIRECTORY).exists()


def test_a_pinned_fetched_commit_plans_offline(tmp_path, remote, git_log):
    design_file = _design_file(tmp_path, _pinned(remote))
    _plan(tmp_path, design_file)
    started = len(git_log())
    remote.moved_away()
    _plan(tmp_path, design_file)
    assert len(git_log()) == started


def test_a_branch_offline_is_fetch_failed(tmp_path, remote):
    design_file = _design_file(
        tmp_path, {"uri": f"git+{remote.url}?branch=main#dep.yaml", "rtl": {"pos": -1}}
    )
    _plan(tmp_path, design_file)
    remote.moved_away()
    with pytest.raises(DesignError) as failed:
        _plan(tmp_path, design_file)
    assert failed.value.code == "fetch_failed" and failed.value.field == "dependencies[0]"
    assert "pin" in str(failed.value) and "main" in str(failed.value)


def test_a_failed_fetch_leaves_no_storage(tmp_path, remote):
    missing = "0" * 40
    design_file = _design_file(tmp_path, _pinned(remote, missing))
    with pytest.raises(DesignError) as failed:
        _plan(tmp_path, design_file)
    assert failed.value.code == "fetch_failed"
    assert list((tmp_path / "xeda_run" / DEPENDENCIES_DIRECTORY).glob("[!.]*")) == [] or all(
        p.suffix == ".lock" for p in (tmp_path / "xeda_run" / DEPENDENCIES_DIRECTORY).iterdir()
    )
    assert [
        p for p in (tmp_path / "xeda_run" / DEPENDENCIES_DIRECTORY).iterdir() if p.is_dir()
    ] == []


def test_a_missing_design_file_in_the_repository_is_fetch_failed(tmp_path, remote):
    dependency = {"uri": f"git+{remote.url}?commit={remote.first}#nothing.yaml"}
    with pytest.raises(DesignError) as failed:
        _plan(tmp_path, _design_file(tmp_path, dependency))
    assert failed.value.code == "fetch_failed" and "nothing.yaml" in str(failed.value)


def test_a_branch_is_resolved_to_its_tip_at_every_planning(tmp_path, remote):
    tip = remote.first
    reference = GitReference(uri=f"{remote.url}?branch=main#dep.yaml")
    tmp_path.joinpath("xeda_run").mkdir()
    assert resolve_reference(reference, run_root=tmp_path / "xeda_run") == tip
    moved = remote.advance()
    assert resolve_reference(reference, run_root=tmp_path / "xeda_run") == moved


def test_head_and_a_tag_are_resolved_to_commits(tmp_path, remote):
    run_root = tmp_path / "xeda_run"
    run_root.mkdir()
    _git("tag", "-a", "-m", "annotated", "v1", cwd=remote.work)
    _git("push", "--quiet", "origin", "v1", cwd=remote.work)
    for query in ("", "?branch=v1"):
        reference = GitReference(uri=f"{remote.url}{query}#dep.yaml")
        assert resolve_reference(reference, run_root=run_root) == remote.first


def test_a_pinned_commit_is_resolved_with_no_process(tmp_path, remote, git_log):
    reference = GitReference(uri=f"{remote.url}?commit={remote.first}#dep.yaml")
    assert resolve_reference(reference, run_root=tmp_path) == remote.first
    assert git_log() == []


def test_a_moved_branch_makes_the_node_stale(tmp_path, remote, toy):
    design_file = _design_file(
        tmp_path, {"uri": f"git+{remote.url}?branch=main#dep.yaml", "rtl": {"pos": -1}}
    )
    runner = _runner(tmp_path)
    design = Design.from_file(design_file)
    first = runner.launch_flow(toy, design, {})
    assert first.succeeded and not first.reused
    again = _runner(tmp_path).launch_flow(toy, design, {})
    assert again.reused
    moved = remote.advance()
    changed = _runner(tmp_path).launch_flow(toy, design, {})
    assert not changed.reused
    assert changed.stale_reason == f"dependency {redacted_url(remote.url)} now at {moved[:12]}"


# --------------------------------------------------------------- what a record says


@pytest.fixture
def toy():
    """A flow that copies the text of every source of the design to an output."""
    with preserved_flow_registry():

        class GitToy(Flow):
            """Copies the text of the sources of the design."""

            results_description: ClassVar[dict[str, MetricSpec]] = {}

            class Inputs(Flow.Inputs):
                sources: list[Path] = In(SourceType.Verilog, description="The Verilog sources.")

            def run(self) -> None:
                out = self.run_path / "copied.txt"
                out.write_text("".join(path.read_text() for path in self.inputs.sources))
                self.artifacts.copied = out

        yield GitToy
        registered_flows.pop(GitToy.name, None)


def test_the_record_lists_the_dependency_and_its_commit(tmp_path, remote, toy):
    design = Design.from_file(_design_file(tmp_path, _pinned(remote)))
    runner = _runner(tmp_path)
    flow = runner.launch_flow(toy, design, {})
    assert flow.succeeded
    record = read_record(flow.run_path, run_root=tmp_path / "xeda_run")
    assert [dict(d) for d in record.dependencies] == [
        {
            "repository": remote.url,
            "reference": None,
            "commit": remote.first,
            "design_file": "dep.yaml",
        }
    ]
    sources = {s.path: s.dependency for item in record.inputs for s in item.sources}
    assert sources == {"top.v": None, "src/a.v": 0}


def test_the_identity_does_not_hold_the_commit(tmp_path, remote):
    one = _plan(tmp_path, _design_file(tmp_path, _pinned(remote)))
    # the same files at another commit: the same node, since the commit is freshness evidence
    moved = remote.advance()
    other = tmp_path / "other"
    other.mkdir()
    two = _plan(other, _design_file(other, _pinned(remote, moved)))
    assert one.node(one.requested).flowrun_hash == two.node(two.requested).flowrun_hash


def test_a_parameter_that_is_a_path_in_a_checkout_counts_without_the_commit(tmp_path):
    from xeda.design import DependencyRecord

    (tmp_path / "top.v").write_text("module top; endmodule\n")
    counted = []
    for commit in ("a" * 40, "b" * 40):
        checkout = tmp_path / "xeda_run" / ".dependencies" / commit[:16]
        design = Design(
            name="d",
            design_root=tmp_path,
            rtl={"sources": ["top.v"], "parameters": {"ROM": str(checkout / "rom.mem")}},
        )
        record = DependencyRecord("https://h/u/l.git", None, commit, "dep.yaml", checkout)
        design = design.model_copy(update={"resolved_dependencies": (record,)})
        design.invalidate_cached_properties()
        counted.append(design.part_metadata("rtl")["parameters"])
    assert counted[0] == counted[1] == {"ROM": "$DEPENDENCY_0/rom.mem"}


# ----------------------------------------------------------------- what load refuses


@pytest.mark.parametrize("commit", ["abc1234", "a" * 39, "g" * 40, "a" * 41])
def test_an_abbreviated_commit_is_refused_at_load(tmp_path, commit):
    dependency = f"git+https://example.com/u/lib.git?commit={commit}#lib.yaml"
    with pytest.raises(DesignValidationError, match="full commit hash"):
        Design.from_file(_design_file(tmp_path, dependency))


@pytest.mark.parametrize("key", ["clone_dir", "local_cache"])
def test_a_removed_clone_key_names_its_replacement(tmp_path, key):
    dependency = {"uri": "git+https://example.com/u/lib.git#lib.yaml", key: "somewhere"}
    with pytest.raises(DesignValidationError) as refused:
        Design.from_file(_design_file(tmp_path, dependency))
    assert refused.value.errors[0][3] == "removed_key"
    assert (
        f"`{key}` was removed: use a local dependency that names the design file in a checkout"
        " you manage" in str(refused.value)
    )


def test_a_sha256_commit_is_a_full_hash():
    reference = GitReference(uri=f"https://example.com/u/lib.git?commit={'A' * 64}#lib.yaml")
    assert reference.commit == "a" * 64


# -------------------------------------------------------------------- the credentials

CREDENTIALED = [
    f"git+https://user:{SECRET}@example.com/org/lib.git#design.yaml",
    f"git+https://{SECRET}@example.com/org/lib.git#design.yaml",
]


def test_no_credential_reaches_a_message_a_log_or_a_path(tmp_path, caplog, git_log):
    url = f"file://user:{SECRET}@/does/not/exist.git"
    dependency = f"git+{url}?branch=main#dep.yaml"
    design_file = _design_file(tmp_path, dependency)
    with caplog.at_level(logging.DEBUG), pytest.raises(DesignError) as failed:
        _plan(tmp_path, design_file)
    assert failed.value.code == "fetch_failed"
    assert SECRET not in str(failed.value) and SECRET not in caplog.text
    assert SECRET not in "\n".join(git_log())
    assert [p for p in tmp_path.rglob("*") if SECRET in str(p)] == []


def test_a_fetch_through_a_credential_url_stores_none(tmp_path, remote, caplog):
    credentialed = remote.url.replace("file://", f"file://user:{SECRET}@", 1)
    storage_of = lambda u: dependency_storage(  # noqa: E731
        tmp_path / "xeda_run", dependency_key(u, remote.first)
    )
    assert storage_of(credentialed) == storage_of(remote.url)
    shown = redacted_url(credentialed)
    assert SECRET not in shown and shown.startswith("file://***@")


def test_the_key_names_the_repository_without_its_credentials():
    assert dependency_key(f"https://user:{SECRET}@h/u/l.git", "a" * 40) == dependency_key(
        "https://h/u/l.git", "a" * 40
    )
    assert dependency_key("https://h/u/l.git", "a" * 40) != dependency_key(
        "https://h/u/l.git", "b" * 40
    )
    assert len(dependency_key("https://h/u/l.git", "a" * 40)) == 16


# ----------------------------------------------------------------------- concurrency


def test_two_launches_fetch_one_commit_once(tmp_path, remote, git_log):
    run_root = tmp_path / "xeda_run"
    run_root.mkdir()
    reference = GitReference(uri=f"{remote.url}?commit={remote.first}#dep.yaml")
    storages, errors = [], []
    barrier = threading.Barrier(2)

    def work() -> None:
        try:
            barrier.wait()
            storages.append(fetch(reference, remote.first, run_root=run_root))
        except BaseException as error:  # pylint: disable=broad-except
            errors.append(error)

    threads = [threading.Thread(target=work) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == [] and storages[0] == storages[1]
    assert [line for line in git_log() if " init " in f" {line} "] == [
        "-c core.hooksPath=/dev/null -c maintenance.auto=false -c gc.auto=0 init --quiet ."
    ]


# --------------------------------------------------------------- generators and scrubs


def test_a_local_dependency_that_a_generator_writes_is_refused(tmp_path):
    counter = tmp_path / "generator.log"
    (tmp_path / "gen.py").write_text(f"open({str(counter)!r}, 'a').write('ran\\n')\n")
    spec = {
        "name": "d",
        "rtl": {
            "sources": ["gen/top.v"],
            "generator": {
                "executable": "python3",
                "args": ["$DESIGN_ROOT/gen.py"],
                "sources": ["gen.py"],
            },
        },
        "dependencies": ["gen/dep.yaml"],
    }
    (tmp_path / "d.yaml").write_text(yaml.safe_dump(spec))
    with pytest.raises(DesignError) as refused:
        _plan(tmp_path, tmp_path / "d.yaml")
    assert refused.value.code == "design_file_not_found"
    assert "dep.yaml" in str(refused.value) and "gen.py" in str(refused.value)
    assert not counter.exists()


def test_no_scrub_removes_dependency_storage(tmp_path, remote):
    from xeda.flow_runner.scrub import scrub_design

    design_file = _design_file(tmp_path, _pinned(remote))
    _plan(tmp_path, design_file)
    storage = (
        tmp_path / "xeda_run" / DEPENDENCIES_DIRECTORY / dependency_key(remote.url, remote.first)
    )
    root = tmp_path / "xeda_run"
    scrub_design("yosys_fpga", root / "d", run_root=root, confirm=lambda *_: True)
    assert (storage / "dependency.yaml").is_file()


# -------------------------------------------------- a reference shows no credentials, ever

TOKEN_URL = f"https://user:{SECRET}@example.com/org/lib.git#design.yaml"


def _dependency_design(root: Path, dependency) -> Path:
    return _design_file(root, dependency)


def test_a_url_is_shown_without_its_credentials():
    assert redacted_url(f"https://user:{SECRET}@h:8443/u/lib.git?branch=a#f.toml") == (
        "https://***@h:8443/u/lib.git?branch=a#f.toml"
    )
    assert redacted_url(f"https://us@er:{SECRET}@h/u/lib.git") == "https://***@h/u/lib.git"
    for plain in (
        "https://h/u/lib.git",
        "https://h/u@x/lib.git",
        "git@h:org/lib.git",
        "/srv/x.git",
    ):
        assert redacted_url(plain) == plain


#: The names a Git URL goes by in the modules that read dependencies.
URL_NAMES = {"repo_url", "uri", "uri_str", "design_path"}


def _names_a_url(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Name)
        and node.id in URL_NAMES
        or (isinstance(node, ast.Attribute) and node.attr in URL_NAMES)
    )


@pytest.mark.parametrize("module", [xeda.design, xeda.flow_runner.dependencies])
def test_no_message_shows_a_git_url_as_it_is(module):
    """An f-string or a log call that formats a URL says so through `redacted_url`: the sweep that
    keeps a message added later from showing credentials."""
    source = Path(module.__file__)
    shown: list[str] = []
    for node in ast.walk(ast.parse(source.read_text())):
        values: list[ast.AST] = []
        if isinstance(node, ast.JoinedStr):
            values = [part.value for part in node.values if isinstance(part, ast.FormattedValue)]
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "log"
        ):
            values = list(node.args[1:])
        shown += [f"line {value.lineno}" for value in values if _names_a_url(value)]
    assert shown == [], f"{source.name} shows a URL without `redacted_url`: {shown}"


@pytest.mark.parametrize(
    "dependency", [TOKEN_URL, {"uri": TOKEN_URL}], ids=["a-string", "a-mapping"]
)
def test_a_url_without_git_is_refused_naming_the_git_spelling(tmp_path, dependency):
    with pytest.raises(DesignValidationError) as refused:
        Design.from_file(_dependency_design(tmp_path, dependency))
    message = str(refused.value)
    assert "git+https://***@example.com/org/lib.git#design.yaml" in message
    assert SECRET not in message


def test_a_url_without_git_is_refused_on_the_command_line_without_the_credentials(tmp_path):
    from click.testing import CliRunner

    from xeda.cli import cli

    result = CliRunner().invoke(
        cli, ["run", "yosys", str(_dependency_design(tmp_path, TOKEN_URL)), "--json"]
    )
    assert result.exit_code != 0
    assert "git+https://***@example.com" in result.output
    assert SECRET not in result.output


def test_a_local_design_file_is_still_a_dependency_by_its_path():
    assert DesignReference(uri="lib/lib.toml").uri == "lib/lib.toml"
    assert DesignReference.from_data("../lib/lib.toml").uri == "../lib/lib.toml"
    assert DesignReference.from_data(r"C:\lib\lib.toml").uri == r"C:\lib\lib.toml"


@pytest.mark.parametrize(
    "make",
    [
        lambda: GitReference(uri=TOKEN_URL),
        lambda: GitReference(
            repo_url=f"https://user:{SECRET}@example.com/org/lib.git", design_file="design.yaml"
        ),
        lambda: DesignReference.from_data(f"git+{TOKEN_URL}"),
    ],
    ids=["uri", "repo_url", "from_data"],
)
def test_a_reference_is_shown_without_the_credentials_of_its_url(make):
    reference = make()
    for text in (
        repr(reference),
        str(reference),
        f"{reference}",
        f"{reference!r}",
        "%s" % (reference,),
        repr([reference]),
        repr({"dependencies": [reference]}),
    ):
        assert SECRET not in text and "https://***@example.com/org/lib.git" in text, text
    # what a fetch needs is still in it
    assert SECRET in reference.uri and SECRET in reference.repo_url  # type: ignore[attr-defined]


def test_a_reference_that_is_a_path_is_shown_as_it_is():
    assert "lib/lib.toml" in repr(DesignReference(uri="lib/lib.toml"))


def test_the_debug_dump_masks_the_references_an_api_caller_gives(tmp_path, caplog):
    reference = GitReference(
        uri=f"https://user:{SECRET}@example.com/u/lib.git?commit={'a' * 40}#lib.toml"
    )
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    with caplog.at_level(logging.DEBUG):
        Design(
            name="d",
            design_root=tmp_path,
            rtl={"sources": ["top.v"], "top": "top"},
            dependencies=[reference],
        )
    assert "Design data" in caplog.text
    assert SECRET not in caplog.text


CRAFTED = [
    "https://h/u/lib.git",
    "no-scheme/u/lib.git#lib.toml",
]


@pytest.mark.parametrize("uri", CRAFTED)
def test_a_url_that_names_no_design_file_or_no_host_is_refused(uri):
    with pytest.raises(ValidationError):
        GitReference(uri=uri)


def test_the_command_line_reports_a_failed_fetch_as_a_spec_that_never_started(tmp_path, git_log):
    from click.testing import CliRunner

    from xeda.cli import cli

    dependency = f"git+{(tmp_path / 'nowhere.git').as_uri()}?branch=main#dep.yaml"
    design_file = _design_file(tmp_path, dependency)
    result = CliRunner().invoke(
        cli,
        [
            "run",
            "yosys_fpga",
            str(design_file),
            "--dry-run",
            "--json",
            "--run-root",
            str(tmp_path / "xeda_run"),
        ],
    )
    document = json.loads(result.stdout)
    assert result.exit_code == 2
    assert document["outcome"] is None and document["error"]["code"] == "fetch_failed"


def test_a_remote_package_carries_the_references_and_no_fetched_file(tmp_path, remote):
    from pathlib import PurePosixPath

    from xeda.flow_runner.dependencies import merged_design
    from xeda.remote.packaging import package_inputs

    design = Design.from_file(_design_file(tmp_path, _pinned(remote)))
    package = package_inputs(
        merged_design(design, run_root=None, git=False),
        {},
        {"yosys_fpga"},
        run_root=tmp_path / "run_root",
        remote_inputs=PurePosixPath(tmp_path / "remote_inputs"),
    )
    document = json.loads(package.generated[package.design_file])
    assert [d["repo_url"] for d in document["dependencies"]] == [remote.url]
    assert [d["commit"] for d in document["dependencies"]] == [remote.first]
    assert sorted(package.files) == ["top.v"]
    assert not (tmp_path / "xeda_run").exists(), "this side fetched nothing"


@pytest.mark.skipif(os.name == "nt", reason="no storage locks")
def test_a_launch_holds_the_lease_of_the_dependency_storage_while_a_flow_runs(
    tmp_path, remote, toy, monkeypatch
):
    import fcntl

    from xeda.flow_runner.run_lock import lock_file

    storage = (
        tmp_path / "xeda_run" / DEPENDENCIES_DIRECTORY / dependency_key(remote.url, remote.first)
    )
    refused: list[bool] = []
    run = toy.run

    def run_and_try_to_write(self) -> None:
        with open(lock_file(storage), "a") as other:
            try:
                fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                refused.append(False)
            except BlockingIOError:
                refused.append(True)
        run(self)

    monkeypatch.setattr(toy, "run", run_and_try_to_write)
    design = Design.from_file(_design_file(tmp_path, _pinned(remote)))
    assert _runner(tmp_path).launch_flow(toy, design, {}).succeeded
    assert refused == [True]
    # the lease ends with the launch
    with open(lock_file(storage), "a") as other:
        fcntl.flock(other.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def test_an_unknown_branch_is_fetch_failed_without_the_offline_advice(tmp_path, remote):
    run_root = tmp_path / "xeda_run"
    run_root.mkdir()
    reference = GitReference(uri=f"{remote.url}?branch=nothing#dep.yaml")
    with pytest.raises(DesignError, match="no such branch or tag") as failed:
        resolve_reference(reference, run_root=run_root, field="dependencies[2]")
    assert failed.value.code == "fetch_failed" and failed.value.field == "dependencies[2]"
    assert "pin" not in str(failed.value)


# ---------------------------------------------- local and Git dependencies in one design


def _mixed(root: Path, remote: Remote, order: str = "local first") -> Path:
    """A design with a local dependency and a Git dependency, in the order given."""
    lib = root / "lib"
    lib.mkdir()
    (lib / "b.v").write_text("module b; endmodule\n")
    (lib / "lib.yaml").write_text(yaml.safe_dump({"name": "lib", "rtl": {"sources": ["b.v"]}}))
    local = {"uri": "lib/lib.yaml"}
    git = {"uri": f"git+{remote.url}?commit={remote.first}#dep.yaml"}
    (root / "top.v").write_text("module top; endmodule\n")
    spec = {
        "name": "d",
        "rtl": {"sources": ["top.v"], "clocks": ["clk"]},
        "dependencies": [local, git] if order == "local first" else [git, local],
    }
    (root / "d.yaml").write_text(yaml.safe_dump(spec))
    return root / "d.yaml"


def _names(plan) -> list[str]:
    return [Path(s["path"]).name for s in _configuration(plan)["rtl"]["sources"]]


def test_an_index_is_the_position_in_the_dependencies_of_the_design(tmp_path, remote):
    """The Git dependency is the second entry, so it is dependency 1 in the configuration."""
    plan = _plan(tmp_path, _mixed(tmp_path, remote, "local first"))
    sources = _configuration(plan)["rtl"]["sources"]
    assert {"dependency": 1, "path": "src/a.v", "type": "Verilog"} in sources
    assert {"path": "lib/b.v", "type": "Verilog"} in sources


def test_dependencies_merge_in_the_order_they_are_listed(tmp_path, remote):
    """A source list with `pos: 0` puts the later dependency first, whichever kind it is."""
    local_first = _plan(tmp_path / "one", _mixed(_dir(tmp_path / "one"), remote, "local first"))
    git_first = _plan(tmp_path / "two", _mixed(_dir(tmp_path / "two"), remote, "git first"))
    assert _names(local_first) == ["a.v", "b.v", "top.v"]
    assert _names(git_first) == ["b.v", "a.v", "top.v"]


def _dir(path: Path) -> Path:
    path.mkdir()
    return path


def test_the_record_lists_every_entry_at_its_index(tmp_path, remote, toy):
    design = Design.from_file(_mixed(tmp_path, remote, "git first"))
    flow = _runner(tmp_path).launch_flow(toy, design, {})
    assert flow.succeeded
    record = read_record(flow.run_path, run_root=tmp_path / "xeda_run")
    assert [dict(d) for d in record.dependencies] == [
        {
            "repository": remote.url,
            "reference": None,
            "commit": remote.first,
            "design_file": "dep.yaml",
        },
        {"repository": None, "reference": None, "commit": None, "design_file": "lib/lib.yaml"},
    ]
    sources = {s.path: s.dependency for item in record.inputs for s in item.sources}
    assert sources == {"top.v": None, "src/a.v": 0, "lib/b.v": None}


def test_a_failed_fetch_names_the_entry_in_the_dependencies(tmp_path, remote):
    design_file = _mixed(tmp_path, remote, "local first")
    spec = yaml.safe_load(design_file.read_text())
    spec["dependencies"][1]["uri"] = spec["dependencies"][1]["uri"].replace(remote.first, "0" * 40)
    design_file.write_text(yaml.safe_dump(spec))
    with pytest.raises(DesignError) as failed:
        _plan(tmp_path, design_file)
    assert failed.value.field == "dependencies[1]"


def test_a_path_in_a_checkout_counts_under_the_index_of_its_entry(tmp_path):
    from xeda.design import DependencyRecord

    (tmp_path / "top.v").write_text("module top; endmodule\n")
    checkout = tmp_path / "xeda_run" / ".dependencies" / "k"
    design = Design(
        name="d",
        design_root=tmp_path,
        rtl={"sources": ["top.v"], "parameters": {"ROM": str(checkout / "rom.mem")}},
    )
    local = DependencyRecord(None, None, None, "lib/lib.yaml", None)
    git = DependencyRecord("https://h/u/l.git", None, "a" * 40, "dep.yaml", checkout)
    design = design.model_copy(update={"resolved_dependencies": (local, git)})
    design.invalidate_cached_properties()
    assert design.part_metadata("rtl")["parameters"] == {"ROM": "$DEPENDENCY_1/rom.mem"}


def test_a_request_that_is_wrong_in_itself_reads_no_dependency(tmp_path, remote, git_log):
    """The settings are checked before any dependency is read: a missing local design file does
    not hide the mistake in the settings, and nothing is fetched."""
    design_file = _mixed(tmp_path, remote, "local first")
    (tmp_path / "lib" / "lib.yaml").unlink()
    with pytest.raises(Exception, match="no_such_setting"):
        _runner(tmp_path).plan(
            "yosys_fpga", str(design_file), flow_settings={**XILINX, "no_such_setting": 1}
        )
    assert git_log() == []
