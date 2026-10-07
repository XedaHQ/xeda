"""Changelog fragments: one file per change in `changelog.d/`, folded into `CHANGELOG.md` later.

`tools/fold_changelog.py` holds the one rule for what a fragment is, and the fold. These tests
check that every fragment of the checkout follows the rule (so a misnamed file fails here instead
of vanishing at release), that the rule rejects each way to break it, and that the fold puts the
entries where the layout of `CHANGELOG.md` wants them.
"""

import datetime
import shutil
import subprocess
import sys
import types
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).parent.parent
TOOL = ROOT / "tools" / "fold_changelog.py"


def _load_tool() -> types.ModuleType:
    """The script as a module. `tools/` is no package, and importing the file would write its
    bytecode into the checkout, so its source runs in a module of its own."""
    module = types.ModuleType("fold_changelog")
    module.__file__ = str(TOOL)
    exec(compile(TOOL.read_text(encoding="utf-8"), str(TOOL), "exec"), module.__dict__)
    return module


tool = _load_tool()

ONE_LINE = "- `xeda scrub` keeps a run that finished after it listed the directories.\n"
NOT_A_FRAGMENT_NAME = (
    f"the name must be <slug>.<type>.md, and <type> one of {', '.join(tool.TYPES)} "
    f"(the only file here that is no fragment is {tool.KEEP_FILE})"
)


def write(directory: Path, name: str, content: str | bytes) -> Path:
    path = directory / name
    path.write_bytes(content if isinstance(content, bytes) else content.encode("utf-8"))
    return path


def files_of(directory: Path) -> dict[Path, bytes]:
    return {path: path.read_bytes() for path in sorted(directory.rglob("*")) if path.is_file()}


# ------------------------------------------------------------------------- the checkout's files


def test_every_fragment_in_the_checkout_follows_the_convention():
    """A file in `changelog.d/` that breaks the convention would vanish at release. Every file
    there is a fragment, hidden or not, but `.gitkeep`."""
    problems = tool.directory_problems(ROOT / tool.FRAGMENT_DIRECTORY)
    assert not problems, (
        "\n".join(problems)
        + "\nA fragment is <slug>.<type>.md: a kebab-case slug, and a type of fixed, added, changed"
        " or removed. It holds one Markdown bullet. See tools/fold_changelog.py."
    )


def test_the_check_of_a_directory_sees_a_stray_file(tmp_path):
    write(tmp_path, "good-fragment.fixed.md", ONE_LINE)
    write(tmp_path, "notes.txt", ONE_LINE)
    assert tool.directory_problems(tmp_path) == [f"notes.txt: {NOT_A_FRAGMENT_NAME}"]


def test_a_directory_without_fragments_has_no_problem(tmp_path):
    assert tool.directory_problems(tmp_path) == []
    assert tool.directory_problems(tmp_path / "no-such-directory") == []


def test_every_file_but_the_keep_file_must_be_a_fragment_and_they_come_in_the_order_of_slugs(
    tmp_path,
):
    """The walk of the directory skips `.gitkeep` and nothing else: a hidden file is judged, and
    refused, like any other (`directory_problems` names each of them below)."""
    names = ["b.fixed.md", "a-b.fixed.md", "a.added.md", ".hidden-slug.added.md", ".fixed.md"]
    for name in [*names, ".DS_Store", ".gitkeep"]:
        write(tmp_path, name, ONE_LINE)
    assert [path.name for path in tool.fragment_paths(tmp_path)] == [
        ".DS_Store",
        ".fixed.md",
        ".hidden-slug.added.md",
        "a.added.md",
        "a-b.fixed.md",
        "b.fixed.md",
    ]


@pytest.mark.parametrize(
    "name", [".fixed.md", ".hidden-slug.added.md", ".DS_Store", ".gitkeep.md", "README.md"]
)
def test_a_hidden_file_is_refused_like_any_file_that_is_no_fragment(tmp_path, name):
    """Only `.gitkeep` is exempt, so a hidden fragment cannot pass the test and stay unfolded."""
    write(tmp_path, ".gitkeep", "")
    write(tmp_path, "good-fragment.fixed.md", ONE_LINE)
    write(tmp_path, name, ONE_LINE)
    assert tool.directory_problems(tmp_path) == [f"{name}: {NOT_A_FRAGMENT_NAME}"]


def test_the_keep_file_is_exempt_whatever_it_holds(tmp_path):
    write(tmp_path, ".gitkeep", "anything\n")
    assert tool.fragment_paths(tmp_path) == []
    assert tool.directory_problems(tmp_path) == []


def test_a_directory_is_no_fragment(tmp_path):
    (tmp_path / "folder.fixed.md").mkdir()
    assert tool.directory_problems(tmp_path) == ["folder.fixed.md: is not a file"]


# ----------------------------------------------------------------------------- what a fragment is


@pytest.mark.parametrize(
    "name",
    [
        "scrub.fixed.md",
        "scrub-keeps-new-runs.fixed.md",
        "ipv6.added.md",
        "a-1-b-2.changed.md",
        "gone.removed.md",
    ],
)
def test_a_good_name_has_no_problem(tmp_path, name):
    assert tool.fragment_problems(write(tmp_path, name, ONE_LINE)) == []


@pytest.mark.parametrize(
    "text",
    [
        ONE_LINE,
        "- First line.\n  Second line, indented by two spaces.\n  Third line.\n",
        "- " + "x" * 98 + "\n",
        "- `code`, **bold**, a [link](https://example.org) and a - dash.\n",
    ],
    ids=["one line", "continuation lines", "exactly 100 columns", "inline Markdown"],
)
def test_a_good_text_has_no_problem(tmp_path, text):
    assert tool.fragment_problems(write(tmp_path, "good.fixed.md", text)) == []


@pytest.mark.parametrize(
    "name,message",
    [
        ("Scrub.fixed.md", "the slug `Scrub` must be kebab-case"),
        ("scrub_runs.fixed.md", "the slug `scrub_runs` must be kebab-case"),
        ("scrub--runs.fixed.md", "must be kebab-case"),
        ("-scrub.fixed.md", "must be kebab-case"),
        ("scrub-.fixed.md", "must be kebab-case"),
        ("scrub runs.fixed.md", "must be kebab-case"),
        ("scrub.fix.md", "the type `fix` must be one of fixed, added, changed, removed"),
        ("scrub.Fixed.md", "the type `Fixed` must be one of fixed, added, changed, removed"),
        ("scrub.feature.md", "the type `feature` must be one of fixed, added, changed, removed"),
        ("scrub.md", NOT_A_FRAGMENT_NAME),
        ("scrub.runs.fixed.md", NOT_A_FRAGMENT_NAME),
        (".fixed.md", NOT_A_FRAGMENT_NAME),
        ("scrub.fixed.txt", NOT_A_FRAGMENT_NAME),
        ("scrub.fixed.md.orig", NOT_A_FRAGMENT_NAME),
        ("scrub.fixed", NOT_A_FRAGMENT_NAME),
        ("README.md", NOT_A_FRAGMENT_NAME),
    ],
)
def test_a_bad_name_is_refused(tmp_path, name, message):
    (problem,) = tool.fragment_problems(write(tmp_path, name, ONE_LINE))
    assert problem.startswith(f"{name}: ")
    assert message in problem


@pytest.mark.parametrize("name", ["scrub.fix.md", "scrub.Fixed.md", "scrub.FIXED.md"])
def test_a_wrong_type_says_the_right_one(tmp_path, name):
    (problem,) = tool.fragment_problems(write(tmp_path, name, ONE_LINE))
    assert "(did you mean `fixed`?)" in problem


@pytest.mark.parametrize(
    "content,message",
    [
        ("", "is empty"),
        ("\n", "is empty"),
        ("Fixed a thing.\n", "must start with `- `"),
        ("* Fixed a thing.\n", "must start with `- `"),
        ("-Fixed a thing.\n", "must start with `- `"),
        ("-  Fixed a thing.\n", "must start with `- `"),
        ("- One.\n- Two.\n", "line 2 must start with exactly two spaces"),
        ("- One\ncontinues.\n", "line 2 must start with exactly two spaces"),
        ("- One\n    continues.\n", "line 2 must start with exactly two spaces"),
        ("- One.\n\n  Two.\n", "line 2 is blank"),
        ("- One.\n\n", "line 2 is blank"),
        ("- " + "x" * 99 + "\n", "line 1 has 101 columns: wrap at 100"),
        ("- One\n  " + "x" * 99 + "\n", "line 2 has 101 columns: wrap at 100"),
        ("- One. \n", "line 1 ends with a space"),
        ("- One\n  two. \n", "line 2 ends with a space"),
        ("- One.", "does not end with a newline"),
        ("- One\tTwo.\n", "has a tab"),
        (b"- One.\r\n", "has a carriage return"),
        (b"- One \xff\n", "is not UTF-8 text"),
    ],
    ids=[
        "empty",
        "only a newline",
        "no bullet",
        "star bullet",
        "no space after the dash",
        "two spaces after the dash",
        "second bullet",
        "unindented continuation",
        "continuation of four spaces",
        "blank line inside",
        "blank line at the end",
        "first line too long",
        "continuation too long",
        "trailing space",
        "trailing space of a continuation",
        "no newline at the end",
        "tab",
        "carriage return",
        "not UTF-8",
    ],
)
def test_a_bad_text_is_refused(tmp_path, content, message):
    problems = tool.fragment_problems(write(tmp_path, "scrub.fixed.md", content))
    assert problems, "the text was accepted"
    assert all(problem.startswith("scrub.fixed.md: ") for problem in problems)
    assert any(message in problem for problem in problems), problems


def test_every_problem_of_a_file_is_reported_not_only_the_first(tmp_path):
    problems = tool.fragment_problems(write(tmp_path, "Scrub.fix.md", "no bullet\n\n- two\n"))
    assert len(problems) == 5, problems


# ------------------------------------------------------------------------------------- the fold

CHANGELOG = """\
# Changelog
The intro.


## [Unreleased]

### Fixed
- Old fix.
  Its second line.

### Changed
- Old change.


## [v0.4.3] - 2026-09-29

### Fixed
- Older fix.
"""

ENTRIES = {
    "fixed": ["- New fix.\n  Its second line."],
    "added": ["- New addition."],
    "changed": ["- New change one.", "- New change two."],
    "removed": ["- New removal."],
}


def fold(changelog=CHANGELOG, entries=ENTRIES, version="v0.5.0", date="2026-10-31"):
    return tool.fold(changelog, entries, version, date)


def test_the_fold_adds_the_entries_to_the_lists_of_the_unreleased_section():
    """To the end of a list that exists. A list that does not exist is made in the order of the
    types: `### Added` before `### Changed`, a later type, and `### Removed` after the last list."""
    assert fold() == """\
# Changelog
The intro.


## [v0.5.0] - 2026-10-31

### Fixed
- Old fix.
  Its second line.
- New fix.
  Its second line.

### Added
- New addition.

### Changed
- Old change.
- New change one.
- New change two.

### Removed
- New removal.


## [v0.4.3] - 2026-09-29

### Fixed
- Older fix.
"""


def test_the_fold_loses_nothing_and_adds_only_what_it_names():
    old, new = Counter(CHANGELOG.split("\n")), Counter(fold().split("\n"))
    assert old - new == {"## [Unreleased]": 1}
    assert new - old == {
        "## [v0.5.0] - 2026-10-31": 1,
        "### Added": 1,
        "### Removed": 1,
        "": 2,
        "- New fix.": 1,
        "  Its second line.": 1,
        "- New addition.": 1,
        "- New change one.": 1,
        "- New change two.": 1,
        "- New removal.": 1,
    }


def test_the_fold_makes_a_section_above_the_newest_release_if_there_is_no_unreleased_one():
    changelog = CHANGELOG.replace("## [Unreleased]", "## [v0.4.4] - 2026-10-01")
    new = fold(changelog, {"fixed": ["- New fix."], "removed": ["- New removal."]})
    assert new == (
        "# Changelog\nThe intro.\n\n\n"
        "## [v0.5.0] - 2026-10-31\n\n### Fixed\n- New fix.\n\n### Removed\n- New removal.\n\n\n"
        + changelog[changelog.index("## [v0.4.4]") :]
    )


def test_the_fold_fills_an_empty_unreleased_section():
    changelog = (
        "# Changelog\n\n\n## [Unreleased]\n\n\n## [v0.4.3] - 2026-09-29\n\n### Fixed\n- A.\n"
    )
    assert fold(changelog, {"added": ["- New."]}) == (
        "# Changelog\n\n\n## [v0.5.0] - 2026-10-31\n\n### Added\n- New.\n\n\n"
        "## [v0.4.3] - 2026-09-29\n\n### Fixed\n- A.\n"
    )


def test_the_fold_fills_an_unreleased_section_that_ends_the_file():
    changelog = "# Changelog\n\n## [Unreleased]\n\n### Fixed\n- Old.\n"
    assert fold(changelog, {"fixed": ["- New."], "added": ["- Added."]}) == (
        "# Changelog\n\n## [v0.5.0] - 2026-10-31\n\n### Fixed\n- Old.\n- New.\n\n"
        "### Added\n- Added.\n"
    )


def test_the_fold_keeps_headings_of_other_names_and_the_order_of_the_lists():
    changelog = (
        "## [Unreleased]\n\n### Security\n- Fix.\n\n### Removed\n- Gone.\n\n### Fixed\n- Fixed.\n"
    )
    assert fold(changelog, {"added": ["- New."], "fixed": ["- Newly fixed."]}) == (
        "## [v0.5.0] - 2026-10-31\n\n### Security\n- Fix.\n\n### Added\n- New.\n\n"
        "### Removed\n- Gone.\n\n### Fixed\n- Fixed.\n- Newly fixed.\n"
    )


@pytest.mark.parametrize("version", ["0.5.0", "v0.5", "v0.5.0 ", "V0.5.0", "v0.5.x", "v1.0.0-"])
def test_the_fold_refuses_a_version_the_tags_do_not_spell(version):
    with pytest.raises(tool.ChangelogError, match="is not a version as the tags spell it"):
        fold(version=version)


@pytest.mark.parametrize(
    "version", ["v0.5.0", "v1.20.3", "v0.1.0-alpha.11", "v0.5.0rc1", "v2.0.0.1", "v0.5.0+local"]
)
def test_the_fold_takes_a_version_the_tags_spell(version):
    assert f"\n## [{version}] - 2026-10-31\n" in fold(version=version)


@pytest.mark.parametrize(
    "date", ["20261031", "31-10-2026", "2026-10-1", "2026-13-01", "2026-02-30"]
)
def test_the_fold_refuses_a_date_that_is_no_date(date):
    with pytest.raises(tool.ChangelogError, match="is not a date"):
        fold(date=date)


@pytest.mark.parametrize(
    "entries", [{}, {"fixed": []}, {kind: [] for kind in tool.TYPES}], ids=["none", "one", "all"]
)
def test_the_fold_refuses_to_fold_nothing(entries):
    with pytest.raises(tool.ChangelogError, match="there is no fragment"):
        fold(entries=entries)


def test_the_fold_refuses_a_version_that_has_a_section():
    with pytest.raises(tool.ChangelogError, match=r"has a section for v0\.4\.3 already"):
        fold(version="v0.4.3")


def test_the_fold_refuses_two_unreleased_sections():
    with pytest.raises(tool.ChangelogError, match=r"more than one `## \[Unreleased\]` heading"):
        fold(CHANGELOG + "\n## [Unreleased]\n")


@pytest.mark.parametrize(
    "heading", ["## [unreleased]", "## Unreleased", "## [Unreleased] ", "##  [Unreleased]"]
)
def test_the_fold_refuses_an_unreleased_heading_it_would_not_find(heading):
    with pytest.raises(tool.ChangelogError, match=r"write the heading as `## \[Unreleased\]`"):
        fold(CHANGELOG.replace("## [Unreleased]", heading))


def test_the_fold_refuses_a_section_that_has_a_heading_twice():
    """The entries could go to either list, and a release must not carry both."""
    changelog = CHANGELOG.replace("### Changed", "### Fixed\n- Another.\n\n### Changed")
    with pytest.raises(tool.ChangelogError, match="more than once: ### Fixed"):
        fold(changelog)


def test_the_fold_refuses_a_changelog_with_no_release_to_put_a_section_above():
    with pytest.raises(tool.ChangelogError, match="no release section"):
        fold("# Changelog\nThe intro.\n")


def changelog_with(body: str) -> str:
    """A changelog whose `[Unreleased]` section has one list, `### Fixed` (on line 6), and `body`
    for its lines (from line 7 on). An older release follows."""
    return (
        "# Changelog\n\n\n## [Unreleased]\n\n### Fixed\n"
        + body
        + "\n\n\n## [v0.4.3] - 2026-09-29\n\n### Fixed\n- Older fix.\n"
    )


@pytest.mark.parametrize(
    "body,number,line",
    [
        ("- Old fix.\n\nA note about the fixes.", 3, "A note about the fixes."),
        (
            "- Old fix.\n\n[Unreleased]: https://example.org/compare/v0.4.3...HEAD",
            3,
            "[Unreleased]: https://example.org/compare/v0.4.3...HEAD",
        ),
        ("A word before the first bullet.\n- Old fix.", 1, "A word before the first bullet."),
        ("- Old fix.\n* A star bullet.", 2, "* A star bullet."),
        ("- Old fix.\nA line with no indent.", 2, "A line with no indent."),
        ("- Old fix.\n one space", 2, " one space"),
        ("  A continuation of no bullet.\n- Old fix.", 1, "  A continuation of no bullet."),
        ("- Old fix.\n\tA tab.", 2, "\tA tab."),
        ("- Old fix.\n#### A heading", 2, "#### A heading"),
        ("- Old fix.\n<!-- a comment -->", 2, "<!-- a comment -->"),
        ("- Old fix.\n---", 2, "---"),
    ],
    ids=[
        "a note after the last bullet",
        "a link reference after the last bullet",
        "text before the first bullet",
        "a bullet of another kind",
        "an unindented line",
        "an indent of one space",
        "a continuation of no bullet",
        "a tab",
        "a heading of another level",
        "a comment",
        "a rule",
    ],
)
@pytest.mark.parametrize("kind", ["fixed", "added"], ids=["the list that gets entries", "another"])
def test_the_fold_refuses_a_list_that_holds_a_line_that_is_no_bullet(body, number, line, kind):
    """A bullet added after such a line would look like a part of it. The fold refuses the list
    whether it gets an entry or not, so that a new list never follows the line either."""
    with pytest.raises(tool.ChangelogError) as refused:
        fold(changelog_with(body), {kind: ["- New."]})
    message = str(refused.value)
    assert f"line {6 + number} of CHANGELOG.md" in message
    assert repr(line) in message
    assert "in the list `### Fixed` of the section `[Unreleased]`" in message


def test_the_fold_shortens_a_long_line_it_names():
    body = "- Old fix.\n\n" + "x" * 100
    with pytest.raises(tool.ChangelogError) as refused:
        fold(changelog_with(body))
    assert f"({'x' * 57 + '...'!r})" in str(refused.value)


def test_the_fold_accepts_blank_lines_and_continuations_in_a_list():
    body = (
        "- Old fix.\n  Its second line.\n\n  A second paragraph of that bullet.\n"
        "    - A nested bullet.\n- Another fix."
    )
    assert body + "\n- New fix.\n" in fold(changelog_with(body), {"fixed": ["- New fix."]})


def test_the_fold_does_not_judge_a_list_that_is_not_in_the_section_it_fills():
    changelog = CHANGELOG.replace("- Older fix.\n", "- Older fix.\n\nA note.\n[Unreleased]: x\n")
    new = fold(changelog)
    assert new.endswith("### Fixed\n- Older fix.\n\nA note.\n[Unreleased]: x\n")


def test_the_fold_puts_a_new_list_after_text_that_opens_the_section():
    changelog = "# C\n\n## [Unreleased]\n\nA word about this release.\n\n## [v0.4.3] - 2026-09-29\n"
    assert fold(changelog, {"added": ["- New."]}) == (
        "# C\n\n## [v0.5.0] - 2026-10-31\n\nA word about this release.\n\n### Added\n- New.\n\n"
        "## [v0.4.3] - 2026-09-29\n"
    )


# ---------------------------------------------------------------------------- the script itself


@pytest.fixture
def project(tmp_path):
    """A changelog, and a `changelog.d/` of three fragments and `.gitkeep`."""
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    directory = tmp_path / "changelog.d"
    directory.mkdir()
    write(directory, "scrub-keeps-new-runs.fixed.md", "- Scrub keeps a run.\n  It is new.\n")
    write(directory, "boards.added.md", "- A board.\n")
    write(directory, "gone.removed.md", "- Gone.\n")
    write(directory, ".gitkeep", "")
    return tmp_path


def test_the_script_folds_the_fragments_and_deletes_them(project, capsys):
    assert tool.main(["v0.5.0", "--date", "2026-10-31"], root=project) == 0
    assert (project / "CHANGELOG.md").read_text(encoding="utf-8") == fold(
        entries={
            "fixed": ["- Scrub keeps a run.\n  It is new."],
            "added": ["- A board."],
            "removed": ["- Gone."],
        }
    )
    assert [path.name for path in (project / "changelog.d").iterdir()] == [".gitkeep"]
    assert capsys.readouterr().out == (
        "Folded 3 fragments into CHANGELOG.md, as `## [v0.5.0] - 2026-10-31`.\n"
    )


def test_the_script_says_how_many_fragments_it_folded(tmp_path, capsys):
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    (tmp_path / "changelog.d").mkdir()
    write(tmp_path / "changelog.d", "boards.added.md", "- A board.\n")
    assert tool.main(["v0.5.0", "--date", "2026-10-31"], root=tmp_path) == 0
    assert capsys.readouterr().out == (
        "Folded 1 fragment into CHANGELOG.md, as `## [v0.5.0] - 2026-10-31`.\n"
    )


def test_the_script_dates_the_release_today_by_default(project):
    before = datetime.date.today().isoformat()
    assert tool.main(["v0.5.0"], root=project) == 0
    after = datetime.date.today().isoformat()
    changelog = (project / "CHANGELOG.md").read_text(encoding="utf-8")
    assert f"\n## [v0.5.0] - {before}\n" in changelog or f"\n## [v0.5.0] - {after}\n" in changelog


def test_the_script_changes_nothing_if_a_fragment_is_wrong(project, capsys):
    write(project / "changelog.d", "Bad_Name.fixed.md", "- A fragment.\n")
    before = files_of(project)
    assert tool.main(["v0.5.0", "--date", "2026-10-31"], root=project) == 1
    assert files_of(project) == before
    assert "Bad_Name.fixed.md: the slug `Bad_Name` must be kebab-case" in capsys.readouterr().err


@pytest.mark.parametrize("name", [".hidden-slug.added.md", ".fixed.md", ".DS_Store"])
def test_the_script_refuses_a_hidden_file_that_is_no_fragment_and_changes_nothing(
    project, capsys, name
):
    """A hidden fragment must not stay unfolded, and a hidden stray file is a stray file."""
    write(project / "changelog.d", name, ONE_LINE)
    before = files_of(project)
    assert tool.main(["v0.5.0", "--date", "2026-10-31"], root=project) == 1
    assert files_of(project) == before
    assert f"{name}: the name must be <slug>.<type>.md" in capsys.readouterr().err


def test_the_script_changes_nothing_if_the_changelog_cannot_take_the_entries(project, capsys):
    (project / "CHANGELOG.md").write_text(CHANGELOG + "\n## [Unreleased]\n", encoding="utf-8")
    before = files_of(project)
    assert tool.main(["v0.5.0", "--date", "2026-10-31"], root=project) == 1
    assert files_of(project) == before
    assert "error: CHANGELOG.md has more than one" in capsys.readouterr().err


def test_the_script_changes_nothing_if_a_list_of_the_changelog_holds_a_note(project, capsys):
    (project / "CHANGELOG.md").write_text(
        CHANGELOG.replace("- Old change.\n", "- Old change.\n\nA note.\n"), encoding="utf-8"
    )
    before = files_of(project)
    assert tool.main(["v0.5.0", "--date", "2026-10-31"], root=project) == 1
    assert files_of(project) == before
    assert "error: line 14 of CHANGELOG.md ('A note.') is in the list `### Changed`" in (
        capsys.readouterr().err
    )


@pytest.mark.parametrize("directory", [False, True], ids=["no directory", "only .gitkeep"])
def test_the_script_refuses_to_fold_nothing(tmp_path, capsys, directory):
    (tmp_path / "CHANGELOG.md").write_text(CHANGELOG, encoding="utf-8")
    if directory:
        (tmp_path / "changelog.d").mkdir()
        write(tmp_path / "changelog.d", ".gitkeep", "")
    assert tool.main(["v0.5.0"], root=tmp_path) == 1
    assert "error: there is no fragment in changelog.d/ to fold" in capsys.readouterr().err
    assert (tmp_path / "CHANGELOG.md").read_text(encoding="utf-8") == CHANGELOG


def test_the_script_refuses_a_missing_changelog(project, capsys):
    (project / "CHANGELOG.md").unlink()
    assert tool.main(["v0.5.0"], root=project) == 1
    assert "error: there is no CHANGELOG.md in" in capsys.readouterr().err
    assert len(list((project / "changelog.d").iterdir())) == 4


def test_the_script_runs_from_the_command_line(project):
    """The documented command, in a copy of the repository's layout: the script finds
    `CHANGELOG.md` and `changelog.d/` from where it is."""
    (project / "tools").mkdir()
    shutil.copy(TOOL, project / "tools")
    done = subprocess.run(
        [sys.executable, "-I", "tools/fold_changelog.py", "v0.5.0", "--date", "2026-10-31"],
        cwd=project,
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stderr
    assert "\n## [v0.5.0] - 2026-10-31\n" in (project / "CHANGELOG.md").read_text(encoding="utf-8")
    assert [path.name for path in (project / "changelog.d").iterdir()] == [".gitkeep"]


# ------------------------------------------------------------------------ the real CHANGELOG.md


def _sections(text: str) -> list[tuple[str, list[str]]]:
    """The `## ` sections of a changelog as (heading, lines), in order."""
    sections: list[tuple[str, list[str]]] = []
    for line in text.split("\n"):
        if line.startswith("## "):
            sections.append((line, []))
        elif sections:
            sections[-1][1].append(line)
    return sections


def test_no_release_and_no_section_of_the_changelog_has_a_heading_twice():
    """A release has one list of fixes, one of additions, and so on. A second `### Added` is how
    an entry gets lost when two changes are merged, and the fold could not tell which to fill. The
    same goes for two `## [Unreleased]` sections."""
    sections = _sections((ROOT / "CHANGELOG.md").read_text(encoding="utf-8"))
    assert len(sections) > 1
    headings = Counter(heading for heading, _ in sections)
    assert [heading for heading, count in headings.items() if count > 1] == []
    for heading, lines in sections:
        subheadings = Counter(line for line in lines if line.startswith("### "))
        repeated = [subheading for subheading, count in subheadings.items() if count > 1]
        assert not repeated, f"{heading} has these headings more than once: {repeated}"


def test_the_fold_can_take_entries_into_the_real_changelog():
    """In whatever state `CHANGELOG.md` is in (an `[Unreleased]` section, or the newest release
    first), the fold keeps every line and puts the entries in the section of the new release."""
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    entries = {kind: [f"- An entry of type {kind}."] for kind in tool.TYPES}
    new = tool.fold(changelog, entries, "v99.0.0", "2099-01-01")
    lost = Counter(changelog.split("\n")) - Counter(new.split("\n"))
    assert lost in (Counter(), Counter({"## [Unreleased]": 1}))
    (section,) = [
        lines for heading, lines in _sections(new) if heading == "## [v99.0.0] - 2099-01-01"
    ]
    for kind in tool.TYPES:
        assert new.count(f"\n- An entry of type {kind}.\n") == 1
        assert f"- An entry of type {kind}." in section
