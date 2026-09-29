"""Git dependencies are cloned into the run root, never into the start directory."""

from pathlib import Path

import pytest

from xeda import Design
from xeda.design import DesignValidationError, cloning_dependencies_into
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
    with cloning_dependencies_into(lambda: runner.run_root / ".dependencies"):
        design = Design.from_file(_design_file(tmp_path))
    assert clones == [tmp_path / "xeda_run" / ".dependencies" / "example.com" / "u/lib.git"]
    assert not (tmp_path / ".xeda_dependencies").exists()
    assert any(src.path.name == "lib.v" for src in design.rtl.sources)


def test_a_design_without_git_dependencies_makes_no_run_root(tmp_path, monkeypatch, clones):
    """Opus I1: the cache is a provider, called only to clone -- the lazy run root stays unmade."""
    monkeypatch.chdir(tmp_path)
    (tmp_path / "top.v").write_text("module top; endmodule\n")
    runner = DefaultRunner(tmp_path / "xeda_run", display_results=False)
    with cloning_dependencies_into(lambda: runner.run_root / ".dependencies"):
        Design(name="d", design_root=tmp_path, rtl={"sources": ["top.v"], "top": "top"})
    assert clones == [] and not (tmp_path / "xeda_run").exists()


def test_outside_a_launcher_a_git_dependency_needs_its_clone_directory(
    tmp_path, monkeypatch, clones
):
    monkeypatch.chdir(tmp_path)
    with pytest.raises(DesignValidationError, match="clone_dir"):
        Design.from_file(_design_file(tmp_path))
    assert clones == [] and not (tmp_path / ".xeda_dependencies").exists()
