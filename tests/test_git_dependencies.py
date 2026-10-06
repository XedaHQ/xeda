"""Git dependencies are cloned into the run root, never into the start directory."""

from pathlib import Path

import pytest

from xeda import Design
from xeda.design import DesignValidationError, loading_in_run_root
from xeda.flow_runner import DefaultRunner

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
    ],
)
def test_the_mapping_form_is_refused_the_same_way(fields):
    from pydantic import ValidationError

    from xeda.design import GitReference

    with pytest.raises(ValidationError, match=r"\.\."):
        GitReference(**fields)


def test_a_branch_with_a_slash_is_still_cloned_inside_the_cache(tmp_path, clones):
    from xeda.design import GitReference

    ref = GitReference(
        uri="https://h/u/lib.git?branch=release/1.0#lib.toml", local_cache=tmp_path / "cache"
    )
    assert ref.clone_dir == tmp_path / "cache" / "h" / "u" / "lib.git_release" / "1.0"


def test_a_clone_directory_outside_the_cache_is_refused(tmp_path):
    """Even a reference built without validation cannot clone outside its cache."""
    from xeda.design import GitReference

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

    from xeda.design import GitReference

    monkeypatch.setattr(git.repo.Repo, "clone_from", staticmethod(lambda *a, **k: None))
    ref = GitReference(uri=URI, clone_dir=tmp_path / "clone")
    with pytest.raises(ValueError, match="repo is None"):
        ref.fetch_design()
