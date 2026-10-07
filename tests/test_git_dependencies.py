"""Git dependencies are cloned into the run root, never into the start directory."""

import ast
import hashlib
import itertools
import json
import logging
import os
import re
import subprocess
from pathlib import Path, PureWindowsPath
from types import SimpleNamespace

import pytest
import yaml
from pydantic import ValidationError

import xeda.design
from xeda import Design
from xeda.design import (
    DEPENDENCY_CLONES,
    DesignReference,
    DesignValidationError,
    GitReference,
    clone_location,
    clone_name_parts,
    loading_in_run_root,
    redacted_url,
)
from xeda.flow_runner import DefaultRunner
from xeda.run_dir import RunDirectory, RunDirectoryError

from .tool_utils import require_git

URI = "https://example.com/u/lib.git#lib.toml"
#: The two directories, below a cache, the reference `URI` is cloned into.
HOST, NAME = clone_name_parts("https://example.com/u/lib.git", None, None)


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
        return SimpleNamespace(git=SimpleNamespace(checkout=lambda *_: None))

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
    assert clones == [tmp_path / "xeda_run" / ".dependencies" / HOST / NAME]
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
    pytest.param("https://h/C:/x.git#lib.toml", id="drive-path"),
    pytest.param("https://h/u/lib.git?branch=/etc/x#lib.toml", id="rooted-branch"),
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
    """A branch is part of one name, `release/1.0` makes no directory of its own."""
    ref = GitReference(
        uri="https://h/u/lib.git?branch=release/1.0#lib.toml", local_cache=tmp_path / "cache"
    )
    assert ref.clone_dir.parent == tmp_path / "cache" / "h"
    assert ref.clone_dir.name.startswith("u_lib.git_release_1.0_")


@pytest.mark.parametrize("path", ["/.hidden/x.git", "/..hidden/x.git"])
def test_a_path_that_starts_with_a_dot_is_a_name_not_a_refusal(path):
    """A component that only starts with dots is a name: `..hidden` is not `..`."""
    host, name = clone_name_parts(f"https://h{path}", None, None)
    assert name.startswith("hidden_x.git_")


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
    [".dependencies", f".dependencies/{HOST}", f".dependencies/{HOST}/{NAME}"],
    ids=["cache", "host", "clone-directory"],
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
    assert clone_location(cache, "https://h/u/lib.git", None, "dev", owner=owner) == _location(
        ("https://h/u/lib.git", "dev", None), cache
    )
    cache.mkdir()
    (cache / "h").symlink_to(root)
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
    assert ref.clone_dir == cache / HOST / NAME
    ref.fetch_design()
    assert clones == [cache / HOST / NAME]
    assert (real / HOST / NAME / "lib.toml").is_file()


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
    assert os.path.abspath(ref.clone_dir) == str(tmp_path / HOST / NAME)
    ref.fetch_design()
    assert (tmp_path / HOST / NAME / "lib.toml").is_file()


@pytest.mark.parametrize("field", ["branch", "commit"])
def test_a_branch_or_commit_that_is_not_text_is_reported_at_its_field(field):
    """The text of a branch or commit is checked where it names a directory; what is not text is
    the field's own error, at the field."""
    with pytest.raises(ValidationError) as caught:
        GitReference(repo_url="https://h/u/lib.git", design_file="lib.toml", **{field: 5})
    assert [error["loc"] for error in caught.value.errors()] == [(field,)]


# --- what names a clone: one directory for each thing that is cloned, and no other ---------------

#: Two references that select different clones: what is cloned differs, though the names that
#: used to be made from them (`<path>_<branch>`, `<path>_commit=<commit>`) were the same.
DIFFERENT_CLONES = [
    pytest.param(
        ("https://h/u/lib.git_a/b", None, None),
        ("https://h/u/lib.git", "a/b", None),
        id="a-path-that-ends-like-a-branch",
    ),
    pytest.param(
        ("https://h/u/lib.git_commit=abc", None, None),
        ("https://h/u/lib.git", None, "abc"),
        id="a-path-that-ends-like-a-commit",
    ),
    pytest.param(
        ("https://h/u/lib.git", None, "abc"),
        ("https://h/u/lib.git", "dev", "abc"),
        id="a-commit-with-and-without-a-branch",
    ),
    pytest.param(
        ("https://h/u/lib.git", "a_b", None),
        ("https://h/u/lib.git", "a/b", None),
        id="branches-that-read-alike",
    ),
    pytest.param(
        ("https://h/a_b.git", None, None),
        ("https://h/a/b.git", None, None),
        id="paths-that-read-alike",
    ),
    pytest.param(
        ("https://h/u/lib.git", None, None),
        ("http://h/u/lib.git", None, None),
        id="schemes",
    ),
    pytest.param(
        ("https://h:8443/u/lib.git", None, None),
        ("https://h_8443/u/lib.git", None, None),
        id="a-port-and-an-underscore",
    ),
    pytest.param(
        ("https://h/U/Lib.git", None, None),
        ("https://h/u/lib.git", None, None),
        id="letter-case",
    ),
    pytest.param(
        ("https://h/.x/r.git", None, None),
        ("https://h/x/r.git", None, None),
        id="a-leading-dot",
    ),
]


def _location(identity, cache="/cache"):
    url, branch, commit = identity
    host, name = clone_name_parts(url, commit, branch)
    return Path(cache) / host / name


@pytest.mark.parametrize("one, other", DIFFERENT_CLONES)
def test_references_that_select_different_clones_get_different_directories(one, other):
    """Not even on a file system that ignores letter case."""
    assert str(_location(one)).lower() != str(_location(other)).lower()


def test_two_dependencies_that_select_different_clones_are_cloned_apart(
    tmp_path, monkeypatch, clones
):
    """The second was handed the first's directory, and loaded the design found there."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    dependencies = ["git+https://example.com/u/lib.git_a/b#lib.toml"]
    dependencies += ["git+https://example.com/u/lib.git?branch=a/b#lib.toml"]
    spec = {"name": "d", "rtl": {"sources": ["top.v"], "top": "top"}, "dependencies": dependencies}
    (tmp_path / "d.yaml").write_text(yaml.safe_dump(spec))
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with loading_in_run_root(runner.load_run_root):
        Design.from_file(tmp_path / "d.yaml")
    assert len(clones) == len(set(clones)) == 2


def test_the_same_reference_is_always_cloned_into_the_same_directory(tmp_path):
    """Both spellings of a reference, a URL with a query and its mapping form, name one clone."""
    cache = tmp_path / "cache"
    spelled = GitReference(uri="https://h/u/lib.git?branch=a/b#lib.toml", local_cache=cache)
    mapped = GitReference(
        repo_url="https://h/u/lib.git", branch="a/b", design_file="lib.toml", local_cache=cache
    )
    assert (
        spelled.clone_dir
        == mapped.clone_dir
        == _location(("https://h/u/lib.git", "a/b", None), cache)
    )


def test_a_clone_directory_is_named_by_the_digest_of_what_is_cloned():
    """The name keeps its readable part and ends with a digest of the repository URL, the branch
    and the commit: the whole identity, fixed here so that a clone is found again."""
    url = "https://example.com/u/lib.git"
    assert clone_name_parts(url, None, None) == ("example.com", "u_lib.git_f9390084107191f8")
    assert clone_name_parts(url, None, "release/1.0") == (
        "example.com",
        "u_lib.git_release_1.0_5b03ad24bfaa5a7e",
    )
    digest = hashlib.sha256(json.dumps([url, "dev", "abc123"]).encode()).hexdigest()[:16]
    assert clone_name_parts(url, "abc123", "dev") == (
        "example.com",
        f"u_lib.git_commit_abc123_{digest}",
    )


URLS = [
    "https://h/u/lib.git",
    "https://h/u/lib.git/",
    "https://h/u_lib.git",
    "https://h/u/lib.git_a/b",
    "https://h/u/lib.git_commit=abc",
    "https://h:8443/u/lib.git",
    "https://h_8443/u/lib.git",
    "https://user:pw@h/u/lib.git",
    "http://h/u/lib.git",
    "https://h/.x/r.git",
    "https://h/x/r.git",
    "https://h/U/Lib.git",
    "https://[::1]:8443/u/lib.git",
    "https://h/" + "long/" * 60 + "lib.git",
]
REFS = [None, "a", "a/b", "a_b", "commit=abc", "dev"]


def test_every_clone_directory_is_one_directory_below_its_host_and_all_differ():
    identities = list(itertools.product(URLS, REFS, REFS))
    names = {}
    for url, branch, commit in identities:
        host, name = clone_name_parts(url, commit, branch)
        assert Path(host).parts == (host,) and Path(name).parts == (name,), (url, branch, commit)
        assert len(name) <= 120
        names.setdefault((host.lower(), name.lower()), []).append((url, branch, commit))
    assert [same for same in names.values() if len(same) > 1] == []
    assert len(names) == len(identities)


def test_a_clone_directory_stays_in_its_cache_whatever_the_file_system_calls_a_path():
    """The names a Windows join also keeps below the cache: no drive, no root, no `..`."""
    cache = PureWindowsPath("D:/cache")
    for url, branch, commit in itertools.product(URLS, REFS, REFS):
        host, name = clone_name_parts(url, commit, branch)
        assert cache / host / name == PureWindowsPath("D:/cache", host, name)
        assert (cache / host / name).is_relative_to(cache)


#: Forms that name a place outside the cache where a drive or a root is part of a path, as it is
#: on Windows: `PureWindowsPath("D:/cache") / "C:/x.git"` is `C:/x.git`.
DRIVE_AND_ROOT_FORMS = [
    pytest.param("https://h/C:/x.git", None, None, id="a-drive-as-a-path-component"),
    pytest.param("https://h/a/C:x.git", None, None, id="a-drive-before-a-name"),
    pytest.param("https://h/c:", None, None, id="a-drive-alone"),
    pytest.param("https://h/u/lib.git", "C:/x", None, id="a-drive-in-a-branch"),
    pytest.param("https://h/u/lib.git", None, "C:", id="a-drive-as-a-commit"),
    pytest.param("https://h/u/lib.git", "/abs", None, id="a-branch-with-a-leading-slash"),
    pytest.param("https://h/u/lib.git", None, "/abs", id="a-commit-with-a-leading-slash"),
]


@pytest.mark.parametrize("url, branch, commit", DRIVE_AND_ROOT_FORMS)
def test_a_drive_or_a_root_in_a_name_is_refused_before_any_join(url, branch, commit):
    with pytest.raises(ValueError, match="outside the clone cache"):
        clone_name_parts(url, commit, branch)


def test_a_name_with_nothing_readable_in_it_is_its_digest_below_a_host_directory():
    """Folding can leave nothing of a host or a path (`@`), and a name is never empty."""
    host, name = clone_name_parts("https://@/@@", None, None)
    assert host == "host"
    assert re.fullmatch(r"[0-9a-f]{16}", name)


def test_a_host_and_its_port_are_one_token_and_not_a_drive():
    assert clone_name_parts("https://h:8443/u/lib.git", None, None)[0] == "h_8443"
    assert clone_name_parts("https://[::1]:8443/u/lib.git", None, None)[0] == "1_8443"


@pytest.mark.parametrize("component", ["C:/x.git", "c:x.git", "C:"])
def test_those_forms_are_the_ones_a_windows_join_follows_out_of_a_cache(component):
    cache = PureWindowsPath("D:/cache")
    assert not (cache / "h" / component).is_relative_to(cache)


def test_a_clone_directory_outside_a_cache_under_the_run_root_is_refused_too(tmp_path, monkeypatch):
    """`RunDirectory.unlinked` keeps a name inside the run root; the cache is a smaller place."""
    root = tmp_path.resolve()
    owner = RunDirectory(root, root)
    monkeypatch.setattr("xeda.design.clone_name_parts", lambda *_: ("..", "elsewhere"))
    with pytest.raises(ValueError, match="outside the clone cache"):
        clone_location(root / DEPENDENCY_CLONES, URI, None, None, owner=owner)


# ---------------------------------------------------------------- credentials in a URL

SECRET = "s3cret-token"
#: A URL with credentials in each of the ways they are written, and the same URL without them.
CREDENTIALED = [
    pytest.param(f"https://user:{SECRET}@example.com/u/lib.git", id="user-and-password"),
    pytest.param(f"https://{SECRET}@example.com/u/lib.git", id="a-token-as-the-user"),
    pytest.param(f"https://user:{SECRET}@example.com:8443/u/lib.git", id="with-a-port"),
    pytest.param(f"https://us%40er:{SECRET}@example.com/u/lib.git", id="an-encoded-at-sign"),
    pytest.param(f"https://user:p@{SECRET}@example.com/u/lib.git", id="a-raw-at-sign"),
]


def _without_credentials(url: str) -> str:
    return url.replace(url[url.index("//") + 2 : url.rindex("@") + 1], "")


@pytest.mark.parametrize("url", CREDENTIALED)
def test_the_credentials_of_a_url_are_no_part_of_a_clone_directory_name(url):
    """They would show in every path and log that names the clone."""
    host, name = clone_name_parts(url, None, None)
    plain_host, plain_name = clone_name_parts(_without_credentials(url), None, None)
    assert SECRET not in host + name and "user" not in host + name
    assert host == plain_host, "the host and its port, as for the URL without credentials"
    # the readable part is the same too; only the digest of what is cloned tells them apart
    assert name.rpartition("_")[0] == plain_name.rpartition("_")[0]
    assert name != plain_name


def test_urls_that_differ_only_in_their_credentials_are_cloned_apart():
    one = clone_name_parts("https://one:pw@h/u/lib.git", None, None)
    other = clone_name_parts("https://other:pw@h/u/lib.git", None, None)
    assert one != other and one[0] == other[0]
    assert clone_name_parts("https://h/u/lib.git", None, None) not in (one, other)


def _credentialed_design(root: Path, url: str) -> Path:
    (root / "top.v").write_text("module top; endmodule\n")
    (root / "d.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "d",
                "rtl": {"sources": ["top.v"], "top": "top"},
                "dependencies": [f"git+{url}#lib.toml"],
            }
        )
    )
    return root / "d.yaml"


@pytest.mark.parametrize("url", CREDENTIALED[:3])
def test_a_clone_is_given_its_credentials_and_none_is_shown(
    tmp_path, monkeypatch, clones, caplog, url
):
    """Git is handed the URL as written, since the clone needs it; no path, log line (down to
    the debug dump of the design) or message holds the secret."""
    import git.repo

    handed = []
    clone = git.repo.Repo.clone_from

    def clone_from(url, to_path, **kwargs):
        handed.append(url)
        return clone(url, to_path, **kwargs)

    monkeypatch.setattr(git.repo.Repo, "clone_from", staticmethod(clone_from))
    monkeypatch.chdir(tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with caplog.at_level(logging.DEBUG), loading_in_run_root(runner.load_run_root):
        Design.from_file(_credentialed_design(tmp_path, url))
    assert handed == [url]
    assert "Cloning git repository" in caplog.text and "Design data" in caplog.text
    assert SECRET not in caplog.text
    assert [p for p in (tmp_path / "xeda_run").rglob("*") if SECRET in str(p)] == []
    assert [str(p) for p in clones if SECRET in str(p)] == []


def test_an_error_does_not_show_the_credentials_of_the_url_it_names(tmp_path, monkeypatch):
    url = f"https://user:{SECRET}@example.com/u/lib.git"
    ref = GitReference(repo_url=url, design_file="lib.toml")
    with pytest.raises(ValueError, match="needs a directory") as no_directory:
        ref.fetch_design()
    monkeypatch.setattr("xeda.design.clone_name_parts", lambda *_: ("..", "elsewhere"))
    with pytest.raises(ValueError, match="outside the clone cache") as outside:
        clone_location(tmp_path, url, None, None)
    with pytest.raises(ValueError, match="no repository path") as no_path:
        clone_name_parts(f"https://user:{SECRET}@example.com/", None, None)
    named = (url, url, f"https://user:{SECRET}@example.com/")
    for error, given in zip((no_directory, outside, no_path), named):
        # the message names the URL, with its credentials masked
        assert SECRET not in str(error.value) and redacted_url(given) in str(error.value)


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


#: The names a Git URL, or a value taken from a reference's URI, goes by in `design.py`: the
#: path `DesignReference.fetch_design` makes of it is one.
URL_NAMES = {"repo_url", "uri", "uri_str", "design_path"}


def _names_a_url(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Name)
        and node.id in URL_NAMES
        or (isinstance(node, ast.Attribute) and node.attr in URL_NAMES)
    )


def test_no_message_in_design_py_shows_a_git_url_as_it_is():
    """An f-string or a log call that formats a URL says so through `redacted_url`: the sweep that
    keeps a message added later from showing credentials a clone directory name no longer does."""
    source = Path(xeda.design.__file__)
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
            values = list(node.args[1:])  # the arguments a message is formatted with
        shown += [f"line {value.lineno}" for value in values if _names_a_url(value)]
    assert shown == [], f"{source.name} shows a URL without `redacted_url`: {shown}"


# ------------------------------------- a reference shows its URL without the credentials, always
TOKEN_URL = f"https://user:{SECRET}@example.com/org/lib.git#design.yaml"


def _dependency_design(root: Path, dependency) -> Path:
    (root / "top.v").write_text("module top; endmodule\n")
    (root / "d.yaml").write_text(
        yaml.safe_dump(
            {
                "name": "d",
                "rtl": {"sources": ["top.v"], "top": "top"},
                "dependencies": [dependency],
            }
        )
    )
    return root / "d.yaml"


@pytest.mark.parametrize(
    "dependency", [TOKEN_URL, {"uri": TOKEN_URL}], ids=["a-string", "a-mapping"]
)
def test_a_url_without_git_is_refused_naming_the_git_spelling(tmp_path, monkeypatch, dependency):
    """Without `git+` and `repo_url` a dependency is a local design file, and a URL is none. The
    reference of the base class took it for a path that did not exist, and printed it."""
    monkeypatch.chdir(tmp_path)
    with pytest.raises(DesignValidationError) as refused:
        Design.from_file(_dependency_design(tmp_path, dependency))
    message = str(refused.value)
    assert "git+https://***@example.com/org/lib.git#design.yaml" in message
    assert SECRET not in message


def test_a_url_without_git_is_refused_on_the_command_line_without_the_credentials(
    tmp_path, monkeypatch
):
    from click.testing import CliRunner

    from xeda.cli import cli

    monkeypatch.chdir(tmp_path)
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
    # what the clone needs is still in it
    assert SECRET in reference.uri and SECRET in reference.repo_url  # type: ignore[attr-defined]


def test_a_reference_that_is_a_path_is_shown_as_it_is():
    assert "lib/lib.toml" in repr(DesignReference(uri="lib/lib.toml"))


def test_the_debug_dump_masks_the_references_an_api_caller_gives(
    tmp_path, monkeypatch, clones, caplog
):
    monkeypatch.chdir(tmp_path)
    reference = GitReference(
        uri=f"https://user:{SECRET}@example.com/u/lib.git#lib.toml", local_cache=tmp_path / "cache"
    )
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    with caplog.at_level(logging.DEBUG):
        Design(
            name="d",
            design_root=tmp_path,
            rtl={"sources": ["top.v"], "top": "top"},
            dependencies=[reference],
        )
    assert "Design data" in caplog.text and clones
    assert SECRET not in caplog.text


def test_a_failed_clone_does_not_show_the_credentials(tmp_path, monkeypatch):
    """GitPython hides the user name and the password in the command line it reports."""
    import git.exc
    import git.repo

    def clone_from(url, to_path, **kwargs):
        raise git.exc.GitCommandError(
            ["git", "clone", "-v", "--", url, str(to_path)], 128, "fatal: repository not found"
        )

    monkeypatch.setattr(git.repo.Repo, "clone_from", staticmethod(clone_from))
    monkeypatch.chdir(tmp_path)
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    url = f"https://user:{SECRET}@example.com/u/l.git"
    with (
        loading_in_run_root(runner.load_run_root),
        pytest.raises(git.exc.GitCommandError) as failed,
    ):
        Design.from_file(_credentialed_design(tmp_path, url))
    # GitPython masks the credentials in its own way; the rest of the URL is still named
    assert SECRET not in str(failed.value) and url.partition("@")[2] in str(failed.value)
