"""The installable agent skill.

The skill's prose is packaged; its flow catalog is generated from the installed version, which is
the point -- a hand-maintained catalog drifts. These tests check that the packaged files are
present and well-formed, that the generated catalog actually covers every flow, and that the copy
checked into this repository's `.claude/skills/` has not drifted from the packaged source.
"""

import re
from pathlib import Path

import pytest

from xeda.agent_skill import (
    SKILL_NAME,
    default_skill_dir,
    generate_flows_reference,
    install_skill,
    skill_source_dir,
)
from xeda.introspect import all_flow_classes

REPO_ROOT = Path(__file__).parent.parent
STATIC_REFERENCES = ("design-file.md", "troubleshooting.md")


def test_packaged_skill_files_exist():
    with skill_source_dir() as source:
        assert (source / "SKILL.md").is_file()
        for name in STATIC_REFERENCES:
            assert (source / "references" / name).is_file(), name


def test_skill_md_has_usable_frontmatter():
    """Claude Code needs `name` and `description` in YAML frontmatter to route to a skill."""
    with skill_source_dir() as source:
        text = (source / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    assert match, "SKILL.md must start with YAML frontmatter delimited by ---"
    frontmatter = match.group(1)
    assert re.search(r"^name:\s*xeda\s*$", frontmatter, re.M)
    description = re.search(r"^description:\s*(\S.*)$", frontmatter, re.M)
    assert description, "SKILL.md frontmatter needs a description; it is what triggers the skill"
    # long enough to describe when to use it, short enough to stay in a listing
    assert 80 < len(description.group(1)) < 1024


def test_generated_reference_covers_every_flow():
    reference = generate_flows_reference()
    missing = [cls.name for cls in all_flow_classes() if f"`{cls.name}`" not in reference]
    assert not missing, f"the generated flow catalog omits: {missing}"


def test_generated_reference_reports_dependencies():
    reference = generate_flows_reference()
    assert "`vivado_synth`" in reference
    # the dependency chain is what stops an agent from running the chain by hand
    assert "runs first" in reference


def test_install_skill_writes_prose_and_generated_reference(tmp_path):
    written = install_skill(tmp_path)
    target = tmp_path / SKILL_NAME
    assert (target / "SKILL.md").is_file()
    for name in STATIC_REFERENCES:
        assert (target / "references" / name).is_file()
    flows_md = target / "references" / "flows.md"
    assert flows_md.is_file(), "flows.md must be generated at install time"
    assert "# Flow catalog" in flows_md.read_text(encoding="utf-8")
    assert set(written) == {p for p in target.rglob("*.md")}


def test_install_skill_refuses_to_clobber_without_force(tmp_path):
    install_skill(tmp_path)
    with pytest.raises(FileExistsError, match="--force"):
        install_skill(tmp_path)
    install_skill(tmp_path, force=True)  # explicit opt-in works


def test_default_skill_dir_is_claude_skills(tmp_path):
    assert default_skill_dir(tmp_path) == tmp_path / ".claude" / "skills"


@pytest.mark.parametrize("name", ("SKILL.md",) + tuple(STATIC_REFERENCES))
def test_repo_skill_copy_matches_the_packaged_source(name):
    """`.claude/skills/xeda/` is checked in; it must not drift from `xeda/data/agent/`."""
    installed = REPO_ROOT / ".claude" / "skills" / SKILL_NAME
    relative = "SKILL.md" if name == "SKILL.md" else f"references/{name}"
    checked_in = installed / relative
    if not checked_in.is_file():
        pytest.skip(f"{checked_in} is not present in this checkout")
    with skill_source_dir() as source:
        packaged = source / relative
        packaged_text = packaged.read_text(encoding="utf-8")
    assert checked_in.read_text(encoding="utf-8") == packaged_text, (
        f"{checked_in} differs from the packaged copy. Edit the packaged copy, then re-run "
        "`xeda skill install --force`."
    )


def test_install_works_when_resources_are_not_on_the_filesystem(tmp_path, monkeypatch):
    """Simulates xeda imported from a zip, where `as_file` extracts to a temporary directory.

    `importlib.resources.as_file` only hands back a real package directory when the package lives
    on the filesystem. From a zip it extracts to a temporary directory and deletes it when the
    context exits, so a helper that returned the path from *inside* the `with` gave callers a
    path to something already gone. Copying has to happen while the context is open.
    """
    import contextlib
    import shutil as _shutil
    import tempfile

    with skill_source_dir() as real_source:
        staged = tmp_path / "staged"
        _shutil.copytree(real_source, staged)

    cleaned_up = []

    @contextlib.contextmanager
    def fake_as_file(_traversable):
        extracted = Path(tempfile.mkdtemp(dir=tmp_path))
        _shutil.copytree(staged, extracted / "agent")
        try:
            yield extracted / "agent"
        finally:
            _shutil.rmtree(extracted)
            cleaned_up.append(extracted)

    monkeypatch.setattr("xeda.agent_skill.as_file", fake_as_file)

    written = install_skill(tmp_path / "dest")
    assert cleaned_up, "the simulated extraction context never ran"
    assert (tmp_path / "dest" / SKILL_NAME / "SKILL.md").is_file()
    for path in written:
        assert path.is_file(), path
