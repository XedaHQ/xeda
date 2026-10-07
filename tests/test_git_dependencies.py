"""Git dependencies are cloned into the run root, never into the start directory."""

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml
from pydantic import ValidationError

from xeda import Design
from xeda.design import (
    DEPENDENCY_CLONES,
    DesignValidationError,
    GitReference,
    clone_location,
    loading_in_run_root,
)
from xeda.flow_runner import DefaultRunner
from xeda.run_dir import RunDirectory, RunDirectoryError

from .tool_utils import require_git

URI = "https://example.com/u/lib.git#lib.toml"


@pytest.fixture
def clones(monkeypatch):
    """Stand in for `git clone`: a repository holding a design `lib`; where each clone went."""
    import git.repo

    made = []

    def clone_from(url, to_path, **kwargs):
        to_path = Path(to_path)
        to_path.mkdir(parents=True)
        (to_path / "lib.v").write_text("module lib; endmodule\n")
        (to_path / "lib.toml").write_text('name = "lib"\n[rtl]\nsources = ["lib.v"]\ntop = "lib"\n')
        made.append(to_path)
        return object()

    monkeypatch.setattr(git.repo.Repo, "clone_from", staticmethod(clone_from))
    return made


def _design_file(root: Path) -> Path:
    (root / "top.v").write_text("module top; endmodule\n")
    (root / "d.toml").write_text(
        f'name = "d"\ndependencies = ["git+{URI}"]\n[rtl]\nsources = ["top.v"]\ntop = "top"\n'
    )
    return root / "d.toml"


def test_a_launcher_clones_into_its_run_root(tmp_path, monkeypatch, clones):
    monkeypatch.chdir(tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with loading_in_run_root(runner.load_run_root):
        design = Design.from_file(_design_file(tmp_path))
    assert clones == [tmp_path / "xeda_run" / ".dependencies" / "example.com" / "u/lib.git"]
    assert not (tmp_path / ".xeda_dependencies").exists()
    assert any(src.path.name == "lib.v" for src in design.rtl.sources)


def test_a_design_without_git_dependencies_makes_no_run_root(tmp_path, monkeypatch, clones):
    """The cache is a provider, called only to clone -- the lazy run root stays unmade."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with loading_in_run_root(runner.load_run_root):
        Design(name="d", design_root=tmp_path, rtl={"sources": ["top.v"], "top": "top"})
    assert clones == [] and not (tmp_path / "xeda_run").exists()


def test_outside_a_launcher_a_git_dependency_needs_its_clone_directory(
    tmp_path, monkeypatch, clones
):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(DesignValidationError, match="clone_dir"):
        Design.from_file(_design_file(tmp_path))
    assert clones == [] and not (tmp_path / ".xeda_dependencies").exists()


CRAFTED = [
    pytest.param("https://h/a/../../../x.git#lib.toml", id="dotdot-path"),
    pytest.param("https://h/a/./x.git#lib.toml", id="dot-path"),
    pytest.param("https://../../tmp/x.git#lib.toml", id="dotdot-host"),
    pytest.param("https://h/..#lib.toml", id="only-dotdot"),
    pytest.param("https://h/a\\..\\..\\x.git#lib.toml", id="backslash-path"),
    pytest.param("https://h/#lib.toml", id="empty-path"),
    pytest.param("https://h/u/lib.git?branch=../../../x#lib.toml", id="dotdot-branch"),
    pytest.param("https://h/u/lib.git?branch=a/../../x#lib.toml", id="dotdot-in-branch"),
    pytest.param("https://h/u/lib.git?commit=../../x#lib.toml", id="dotdot-commit"),
    pytest.param("https://h/u/lib.git?commit=a\\b#lib.toml", id="backslash-commit"),
]


@pytest.mark.parametrize("uri", CRAFTED)
def test_a_crafted_git_url_is_refused_before_any_clone(tmp_path, monkeypatch, clones, uri):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    (tmp_path / "d.toml").write_text(
        f'name = "d"\ndependencies = [\'git+{uri}\']\n[rtl]\nsources = ["top.v"]\ntop = "top"\n'
    )
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with loading_in_run_root(runner.load_run_root), pytest.raises(DesignValidationError):
        Design.from_file(tmp_path / "d.toml")
    assert clones == []
    assert {p.name for p in tmp_path.iterdir()} <= {"d.toml", "top.v", "xeda_run"}
    assert not (tmp_path / "xeda_run" / ".dependencies").exists()


@pytest.mark.parametrize(
    "fields",
    [
        {"repo_url": "https://h/a/../../x.git", "design_file": "lib.toml"},
        {"repo_url": "https://../x.git", "design_file": "lib.toml"},
        {"repo_url": "https://h/u/lib.git", "design_file": "lib.toml", "branch": "../x"},
        {"repo_url": "https://h/u/lib.git", "design_file": "lib.toml", "commit": ".."},
        {"repo_url": "https://h/u/li\0b.git", "design_file": "lib.toml"},
    ],
)
def test_the_mapping_form_is_refused_the_same_way(fields):
    with pytest.raises(ValidationError, match=r"\.\."):
        GitReference(**fields)


def test_a_branch_with_a_slash_is_still_cloned_inside_the_cache(tmp_path, clones):
    ref = GitReference(
        uri="https://h/u/lib.git?branch=release/1.0#lib.toml", local_cache=tmp_path / "cache"
    )
    assert ref.clone_dir == tmp_path / "cache" / "h" / "u" / "lib.git_release" / "1.0"


@pytest.mark.parametrize("path", ["/.hidden/x.git", "/..hidden/x.git"])
def test_a_path_that_starts_with_a_dot_keeps_it_in_the_clone_directory(tmp_path, path):
    """A component that only starts with dots is a name: `..hidden` is not `..`."""
    location = clone_location(tmp_path, f"https://h{path}", None, None)
    assert location == tmp_path / "h" / path.lstrip("/")


def test_a_clone_directory_outside_the_cache_is_refused(tmp_path):
    """Even a reference built without validation cannot clone outside its cache."""
    ref = GitReference.model_construct(
        uri="https://h/a/../../../x.git#lib.toml",
        repo_url="https://h/a/../../../x.git",
        design_file="lib.toml",
        local_cache=tmp_path / "cache",
        clone_dir=None,
        commit=None,
        branch=None,
    )
    with pytest.raises(ValueError, match="cache"):
        ref.fetch_design()
    assert not (tmp_path / "x.git").exists()


def test_a_clone_that_yields_no_repository_is_an_error(tmp_path, monkeypatch):
    import git.repo

    monkeypatch.setattr(git.repo.Repo, "clone_from", staticmethod(lambda *a, **k: None))
    ref = GitReference(uri=URI, clone_dir=tmp_path / "clone")
    with pytest.raises(ValueError, match="repo is None"):
        ref.fetch_design()


@pytest.mark.skipif(os.name == "nt", reason="symbolic links need privileges on Windows")
@pytest.mark.parametrize("leads", ["out of the run root", "elsewhere in the run root"])
@pytest.mark.parametrize(
    "link",
    [
        ".dependencies",
        ".dependencies/example.com",
        ".dependencies/example.com/u",
        ".dependencies/example.com/u/lib.git",
    ],
    ids=["cache", "host", "path-part", "clone-directory"],
)
def test_a_clone_is_never_made_through_a_link_in_the_cache(
    tmp_path, monkeypatch, clones, link, leads
):
    """The cache under the run root is named by the rule every cache there follows: no symbolic
    link on the way, not even one that stays inside the run root."""
    monkeypatch.chdir(tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    run_root = runner.load_run_root()
    target = tmp_path / "outside" if leads == "out of the run root" else run_root / "elsewhere"
    target.mkdir()
    planted = run_root / link
    planted.parent.mkdir(parents=True, exist_ok=True)
    planted.symlink_to(target, target_is_directory=True)
    with (
        loading_in_run_root(runner.load_run_root),
        pytest.raises(RunDirectoryError, match=re.escape(f"{planted} is a symbolic link")),
    ):
        Design.from_file(_design_file(tmp_path))
    assert clones == [], "nothing is cloned"
    assert list(target.iterdir()) == [], "nothing is written where the link leads"


def test_a_clone_cache_under_the_run_root_is_named_by_the_unlinked_rule(tmp_path):
    root = tmp_path.resolve()
    owner = RunDirectory(root, root)
    cache = root / DEPENDENCY_CLONES
    assert (
        clone_location(cache, "https://h/u/lib.git", None, "dev", owner=owner)
        == cache / "h" / "u" / "lib.git_dev"
    )
    (cache / "h").mkdir(parents=True)
    (cache / "h" / "u").symlink_to(root)
    with pytest.raises(RunDirectoryError, match="symbolic link where xeda keeps a cache"):
        clone_location(cache, "https://h/u/lib.git", None, None, owner=owner)


def test_a_cache_the_user_names_is_theirs_to_direct(tmp_path, monkeypatch, clones):
    """`local_cache` is the user's own directory: xeda clones and pulls there as told, through a
    link if that is where the user's cache is, and keeps the names it makes inside it."""
    real = tmp_path / "real"
    real.mkdir()
    cache = tmp_path / "cache"
    cache.symlink_to(real, target_is_directory=True)
    ref = GitReference(uri=URI, local_cache=cache)
    assert ref.clone_dir == cache / "example.com" / "u" / "lib.git"
    ref.fetch_design()
    assert clones == [cache / "example.com" / "u" / "lib.git"]
    assert (real / "example.com" / "u" / "lib.git" / "lib.toml").is_file()


def test_the_containment_check_of_a_user_cache_stands_on_its_own(tmp_path, monkeypatch):
    """Whatever names the clone, a directory outside the cache the user named is refused."""
    monkeypatch.setattr("xeda.design.clone_name_parts", lambda *_: ("h", "../../x.git"))
    with pytest.raises(ValueError, match="outside the clone cache"):
        clone_location(tmp_path / "cache", URI, None, None)


@pytest.fixture
def remote_lib(tmp_path, monkeypatch):
    """The design `lib` in a bare repository on this machine, and a git that reads no other
    configuration and reaches no network: `git@fake.invalid:org/lib.git` is rewritten to that
    repository, and an ssh that is started anyway fails at once. The repository."""
    require_git()
    remotes = tmp_path / "remotes"
    bare = remotes / "org" / "lib.git"
    config = tmp_path / "gitconfig"
    config.write_text(
        "[user]\n\tname = Xeda Tests\n\temail = tests@example.invalid\n"
        "[init]\n\tdefaultBranch = main\n"
        "[commit]\n\tgpgsign = false\n"
        f'[url "{remotes}/"]\n\tinsteadOf = git@fake.invalid:\n'
    )
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")
    monkeypatch.setenv("GIT_SSH_COMMAND", "false")
    work = tmp_path / "lib_work"
    work.mkdir()
    (work / "lib.v").write_text("module lib; endmodule\n")
    (work / "lib.yaml").write_text("name: lib\nrtl:\n  sources: [lib.v]\n  top: lib\n")
    for command in (
        ["init", "-q", str(work)],
        ["-C", str(work), "add", "."],
        ["-C", str(work), "commit", "-q", "-m", "lib"],
        ["clone", "-q", "--bare", str(work), str(bare)],
    ):
        subprocess.run(["git", *command], check=True, capture_output=True)
    return bare


@pytest.mark.parametrize(
    "url",
    [
        lambda bare: "git@fake.invalid:org/lib.git",
        lambda bare: bare.as_uri(),
        lambda bare: str(bare),
    ],
    ids=["scp-like", "file-without-host", "local-path"],
)
def test_a_clone_directory_the_user_names_is_used_as_given(tmp_path, monkeypatch, remote_lib, url):
    """Nothing is named from the URL when `clone_dir` is given, so any URL Git takes will do:
    one with no host (`file:///...`, a path) or none in URL form (`user@host:path`)."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    clone = tmp_path / "clone"
    dependency = {"repo_url": url(remote_lib), "design_file": "lib.yaml", "clone_dir": str(clone)}
    spec = {"name": "d", "rtl": {"sources": ["top.v"], "top": "top"}, "dependencies": [dependency]}
    (tmp_path / "d.yaml").write_text(yaml.safe_dump(spec))
    design = Design.from_file(tmp_path / "d.yaml")
    assert (clone / ".git").is_dir()
    assert any(src.path.name == "lib.v" for src in design.rtl.sources)
    assert not (tmp_path / "xeda_run").exists()


@pytest.mark.parametrize(
    "repo_url", ["git@fake.invalid:org/lib.git", "file:///srv/git/lib.git", "/srv/git/lib.git"]
)
def test_a_url_with_no_host_needs_a_clone_directory(repo_url):
    """Without `clone_dir` the host names the clone's directory, so a URL with none is refused."""
    with pytest.raises(ValidationError, match="invalid URL"):
        GitReference(repo_url=repo_url, design_file="lib.toml")


@pytest.mark.parametrize("cache", [".", "./", "", "a/.."])
def test_a_user_cache_that_is_the_current_directory_holds_its_clones(
    tmp_path, monkeypatch, clones, cache
):
    """However it is spelled, a `local_cache` that is the current directory is a cache: its
    clones lie inside it, though a relative path that normalizes to `.` has no common prefix."""
    monkeypatch.chdir(tmp_path)
    ref = GitReference(uri=URI, local_cache=cache)
    assert os.path.abspath(ref.clone_dir) == str(tmp_path / "example.com" / "u" / "lib.git")
    ref.fetch_design()
    assert (tmp_path / "example.com" / "u" / "lib.git" / "lib.toml").is_file()


@pytest.mark.parametrize("field", ["branch", "commit"])
def test_a_branch_or_commit_that_is_not_text_is_reported_at_its_field(field):
    """The text of a branch or commit is checked where it names a directory; what is not text is
    the field's own error, at the field."""
    with pytest.raises(ValidationError) as caught:
        GitReference(repo_url="https://h/u/lib.git", design_file="lib.toml", **{field: 5})
    assert [error["loc"] for error in caught.value.errors()] == [(field,)]
