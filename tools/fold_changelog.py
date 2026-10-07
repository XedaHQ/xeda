"""Fold the fragments in `changelog.d/` into `CHANGELOG.md`: the changelog step of a release.

Each change that a user can see adds one file to `changelog.d/`, so two pull requests never edit
the same lines of `CHANGELOG.md`. At release, run this script from the repository:

    python tools/fold_changelog.py v0.5.0
    python tools/fold_changelog.py v0.5.0 --date 2026-10-31

A fragment is named `<slug>.<type>.md`:

* `<slug>` is a short name for the change in kebab-case: lowercase letters and digits, joined by
  single hyphens. A pull request number is not known when the fragment is written.
* `<type>` is `fixed`, `added`, `changed` or `removed`. It names the heading of the release that
  gets the entry.

The file holds the entry: one Markdown bullet, wrapped at 100 columns. Each line after the first
starts with two spaces. The entry says what changed for a user, in one or two short sentences. An
entry about a breaking change also says what to do instead.

Every file in `changelog.d/` must be a fragment, hidden or not, except `.gitkeep`. That file
keeps the directory in the repository.

The script checks every fragment first, and it changes nothing if one is wrong. Then it adds the
entries, sorted by slug, to the end of the matching `### <Type>` lists of the `## [Unreleased]`
section and renames that section `## [<version>] - <date>`. A changelog with no such section gets
a new section above its newest release. At the end, the script deletes the fragment files.

The script also refuses a `###` list of that section that holds a line other than a bullet
(`- `) or the continuation of a bullet (two spaces), such as a note or a link reference. An entry
added after such a line would look like a part of it.

The script uses only the standard library.
"""

from __future__ import annotations

import argparse
import datetime
import difflib
import re
import sys
from collections import Counter
from collections.abc import Mapping, Sequence
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FRAGMENT_DIRECTORY = "changelog.d"
CHANGELOG = "CHANGELOG.md"

#: The fragment types, in the order of the headings of a release.
TYPES = ("fixed", "added", "changed", "removed")
#: The heading of each type.
HEADINGS = {kind: f"### {kind.capitalize()}" for kind in TYPES}
#: The width that an entry is wrapped at.
MAX_COLUMNS = 100
#: The one file of `changelog.d/` that is no fragment. It keeps the directory in the repository.
KEEP_FILE = ".gitkeep"
UNRELEASED = "## [Unreleased]"

_FILE_NAME = re.compile(r"(?P<slug>[^.]+)\.(?P<type>[^.]+)\.md")
_SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
_FIRST_LINE = re.compile(r"- \S")
_CONTINUATION = re.compile(r"  \S")
_VERSION = re.compile(r"v\d+\.\d+\.\d+(?:[-+.]?[0-9A-Za-z][-+.0-9A-Za-z]*)?")
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")
_UNRELEASED_LIKE = re.compile(r"##\s*\[?unreleased\]?\s*", re.IGNORECASE)


class ChangelogError(Exception):
    """The fragments cannot be folded into the changelog. The message says why."""


# ------------------------------------------------------------------------------ the convention


def fragment_paths(directory: Path) -> list[Path]:
    """The entries of `directory` that must be fragments, by slug. That is every entry, hidden or
    not, but `KEEP_FILE`."""
    if not directory.is_dir():
        return []
    return sorted(
        (path for path in directory.iterdir() if path.name != KEEP_FILE),
        key=lambda path: path.name.split("."),
    )


def directory_problems(directory: Path) -> list[str]:
    """What is wrong with the files of `directory`, as `fragment_problems` words it."""
    return [problem for path in fragment_paths(directory) for problem in fragment_problems(path)]


def fragment_problems(path: Path) -> list[str]:
    """What is wrong with `path` as a fragment, one sentence each. A good fragment has none."""
    problems = _name_problems(path.name)
    if path.is_file():
        problems += _text_problems(path)
    else:
        problems.append("is not a file")
    return [f"{path.name}: {problem}" for problem in problems]


def _name_problems(name: str) -> list[str]:
    match = _FILE_NAME.fullmatch(name)
    if match is None:
        return [
            f"the name must be <slug>.<type>.md, and <type> one of {', '.join(TYPES)} "
            f"(the only file here that is no fragment is {KEEP_FILE})"
        ]
    problems = []
    if not _SLUG.fullmatch(match["slug"]):
        problems.append(
            f"the slug `{match['slug']}` must be kebab-case: lowercase letters and digits, "
            "joined by single hyphens"
        )
    if match["type"] not in TYPES:
        close = difflib.get_close_matches(match["type"].lower(), TYPES, n=1)
        hint = f" (did you mean `{close[0]}`?)" if close else ""
        problems.append(f"the type `{match['type']}` must be one of {', '.join(TYPES)}{hint}")
    return problems


def _text_problems(path: Path) -> list[str]:
    try:
        text = path.read_bytes().decode("utf-8")
    except UnicodeDecodeError:
        return ["is not UTF-8 text"]
    if not text.strip():
        return ["is empty"]
    if "\r" in text:
        return ["has a carriage return: use Unix line ends"]
    problems = []
    if "\t" in text:
        problems.append("has a tab")
    if not text.endswith("\n"):
        problems.append("does not end with a newline")
    lines = (text[:-1] if text.endswith("\n") else text).split("\n")
    if not _FIRST_LINE.match(lines[0]):
        problems.append("must start with `- `: an entry is one Markdown bullet")
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            problems.append(f"line {number} is blank: an entry is one bullet, with no blank line")
        elif number > 1 and not _CONTINUATION.match(line):
            problems.append(
                f"line {number} must start with exactly two spaces, as a continuation of "
                "the bullet (a second bullet is a second fragment)"
            )
        elif line != line.rstrip():
            problems.append(f"line {number} ends with a space")
        if len(line) > MAX_COLUMNS:
            problems.append(f"line {number} has {len(line)} columns: wrap at {MAX_COLUMNS}")
    return problems


# ------------------------------------------------------------------------------------ the fold


def fold(changelog: str, entries: Mapping[str, Sequence[str]], version: str, date: str) -> str:
    """`changelog` with `entries` (fragment type -> bullets) added to the section for `version`.

    The section is the `## [Unreleased]` one, renamed `## [<version>] - <date>`, or a new one
    above the newest release when the changelog has no such section. Each bullet goes to the end
    of its type's `### <Type>` list, which a section without one gets in the order of `TYPES`.
    Everything else stays as it was. A section with a heading twice, or with a list that holds
    anything but bullets, is refused."""
    if not _VERSION.fullmatch(version):
        raise ChangelogError(f"`{version}` is not a version as the tags spell it: use v0.5.0")
    if not _DATE.fullmatch(date):
        raise ChangelogError(f"`{date}` is not a date: use 2026-10-31")
    try:
        datetime.date.fromisoformat(date)
    except ValueError:
        raise ChangelogError(f"`{date}` is not a date of the calendar") from None
    if not any(entries.get(kind) for kind in TYPES):
        raise ChangelogError(f"there is no fragment in {FRAGMENT_DIRECTORY}/ to fold")
    lines = changelog.split("\n")
    if any(line.startswith(f"## [{version}]") for line in lines):
        raise ChangelogError(f"{CHANGELOG} has a section for {version} already")
    start = _section_to_fill(lines)
    _check_headings(lines, start)
    _check_lists(lines, start)
    for kind in TYPES:
        if entries.get(kind):
            _add_entries(
                lines, start, kind, [line for e in entries[kind] for line in e.split("\n")]
            )
    lines[start] = f"## [{version}] - {date}"
    return "\n".join(lines)


def _section_to_fill(lines: list[str]) -> int:
    """The index of the `## [Unreleased]` heading. Without one, make an empty section for it
    above the newest release."""
    found = [i for i, line in enumerate(lines) if _UNRELEASED_LIKE.fullmatch(line)]
    for i in found:
        if lines[i] != UNRELEASED:
            raise ChangelogError(f"write the heading as `{UNRELEASED}`, not as `{lines[i]}`")
    if len(found) > 1:
        raise ChangelogError(f"{CHANGELOG} has more than one `{UNRELEASED}` heading")
    if found:
        return found[0]
    newest = next((i for i, line in enumerate(lines) if line.startswith("## ")), None)
    if newest is None:
        raise ChangelogError(f"{CHANGELOG} has no release section to put the new one above")
    lines[newest:newest] = [UNRELEASED, "", ""]
    return newest


def _section_end(lines: list[str], start: int) -> int:
    """The index of the heading that follows the section at `start`, or the end of `lines`."""
    return next((i for i in range(start + 1, len(lines)) if lines[i].startswith("## ")), len(lines))


def _check_headings(lines: list[str], start: int) -> None:
    """Refuse a section that has a heading twice: the entries could go to either of the lists."""
    headings = Counter(
        line for line in lines[start + 1 : _section_end(lines, start)] if line.startswith("### ")
    )
    repeated = sorted(heading for heading, count in headings.items() if count > 1)
    if repeated:
        raise ChangelogError(
            f"the section {lines[start][3:]} has these headings more than once: "
            f"{', '.join(repeated)}. Merge each pair into one list first."
        )


def _check_lists(lines: list[str], start: int) -> None:
    """Refuse a list that holds a line other than a bullet or the continuation of a bullet.

    The fold adds an entry after the last line of a list. After a note or a link reference at the
    end of the list, the entry would look like a part of that line. With this check, the end of a
    list is always the end of a bullet."""
    heading, in_bullet = "", False
    for index in range(start + 1, _section_end(lines, start)):
        line = lines[index]
        if line.startswith("### "):
            heading, in_bullet = line, False
        elif heading and line.strip():
            if line.startswith("- "):
                in_bullet = True
            elif not (in_bullet and line.startswith("  ")):
                shown = line if len(line) <= 60 else line[:57] + "..."
                raise ChangelogError(
                    f"line {index + 1} of {CHANGELOG} ({shown!r}) is in the list `{heading}` of "
                    f"the section `{lines[start][3:]}`, but it is not a bullet (`- `) and does "
                    "not continue one (two spaces). Move it out of the list first."
                )


def _trim_blanks(lines: list[str], first: int, stop: int) -> int:
    """`stop`, moved back over the blank lines that end `lines[first:stop]`."""
    while stop > first and not lines[stop - 1].strip():
        stop -= 1
    return stop


def _add_entries(lines: list[str], start: int, kind: str, block: list[str]) -> None:
    """Add the `block` of lines to the list of `kind` in the section that starts at `start`."""
    end = _section_end(lines, start)
    heading = next((i for i in range(start + 1, end) if lines[i] == HEADINGS[kind]), None)
    if heading is not None:
        stop = next((i for i in range(heading + 1, end) if lines[i].startswith("### ")), end)
        at = _trim_blanks(lines, heading + 1, stop)
        lines[at:at] = block
        return
    after = {HEADINGS[later] for later in TYPES[TYPES.index(kind) + 1 :]}
    later_heading = next((i for i in range(start + 1, end) if lines[i] in after), None)
    if later_heading is not None:
        lines[later_heading:later_heading] = [HEADINGS[kind], *block, ""]
    else:
        at = _trim_blanks(lines, start + 1, end)
        lines[at:at] = ["", HEADINGS[kind], *block]


# ------------------------------------------------------------------------------------- the CLI


def main(argv: Sequence[str] | None = None, root: Path = ROOT) -> int:
    parser = argparse.ArgumentParser(
        prog="fold_changelog.py",
        description=f"Fold the fragments in {FRAGMENT_DIRECTORY}/ into {CHANGELOG} for a release.",
    )
    parser.add_argument("version", help="the release as its tag spells it, such as v0.5.0")
    parser.add_argument(
        "--date",
        default=datetime.date.today().isoformat(),
        help="the date of the release, as YYYY-MM-DD (default: today)",
    )
    args = parser.parse_args(argv)

    directory = root / FRAGMENT_DIRECTORY
    problems = directory_problems(directory)
    if problems:
        print("\n".join(problems), file=sys.stderr)
        return 1
    paths = fragment_paths(directory)
    entries: dict[str, list[str]] = {kind: [] for kind in TYPES}
    for path in paths:
        entries[path.name.split(".")[-2]].append(path.read_text(encoding="utf-8").rstrip("\n"))

    changelog = root / CHANGELOG
    try:
        if not changelog.is_file():
            raise ChangelogError(f"there is no {CHANGELOG} in {root}")
        text = fold(changelog.read_text(encoding="utf-8"), entries, args.version, args.date)
    except ChangelogError as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    changelog.write_text(text, encoding="utf-8")
    for path in paths:
        path.unlink()
    count = f"{len(paths)} fragment" if len(paths) == 1 else f"{len(paths)} fragments"
    print(f"Folded {count} into {CHANGELOG}, as `## [{args.version}] - {args.date}`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
