#!/usr/bin/env python3
"""Find newer versions of the tools that CI pins, and write them into the workflow files.

CI pins three tools. A pin is one line of a workflow file (two lines for bsc):

* the OSS CAD Suite, by the date of its build: the `version` of the
  `YosysHQ/setup-oss-cad-suite` step in `.github/workflows/ci.yml`;
* the Bluespec Compiler (bsc), by its release and the SHA-256 of its Ubuntu 24.04 tarball:
  `BSC_VERSION` and `BSC_SHA256` in `.github/workflows/ci.yml`;
* the openXC7 toolchain installer, by a commit: `INSTALLER_REV` in
  `.github/workflows/openxc7.yml`.

For each pin, the script asks GitHub for the newest upstream and replaces the pin when upstream is
newer. It never moves a pin back. A workflow file that does not exist has no pin to update, and
the script skips it. A file that exists without its pin, or with the pin in another format, is an
error: the script must change together with the format. Every value that comes from GitHub is
checked against a strict pattern before it reaches a file.

The script uses the standard library only. With `--dry-run` it prints the changes and writes
nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

CI = Path(".github/workflows/ci.yml")
OPENXC7 = Path(".github/workflows/openxc7.yml")
WORKFLOW = "bump-ci-pins.yml"
TITLE = "Update the pinned CI tools"

OSS_CAD_TAG = re.compile(r"\d{4}-\d{2}-\d{2}")
BSC_TAG = re.compile(r"\d{4}\.\d{2}(?:\.\d+)?")
SHA256 = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")

GITHUB = "https://github.com"
OSS_CAD_REPOSITORY = "YosysHQ/oss-cad-suite-build"
BSC_REPOSITORY = "B-Lang-org/bsc"
INSTALLER_REPOSITORY = "openXC7/toolchain-installer"

#: Asks GitHub's REST API for a path and returns the parsed JSON.
Fetch = Callable[[str], Any]
#: Downloads a URL and returns the SHA-256 of its content, in hexadecimal.
Digest = Callable[[str], str]


class PinError(Exception):
    """A pin cannot be read, replaced or checked."""


@dataclass(frozen=True)
class Pin:
    """One tool that CI pins, and where."""

    kind: str
    tool: str
    path: Path
    name: str


PINS = (
    Pin("oss_cad", "OSS CAD Suite", CI, "`version` of the setup-oss-cad-suite step"),
    Pin("bsc", "Bluespec Compiler (bsc)", CI, "`BSC_VERSION` and `BSC_SHA256`"),
    Pin("installer", "openXC7 toolchain installer", OPENXC7, "`INSTALLER_REV`"),
)


@dataclass(frozen=True)
class Update:
    """A pin that upstream has left behind."""

    pin: Pin
    old: str
    new: str
    links: tuple[tuple[str, str], ...]
    sha256: str = ""  # of the bsc tarball


# ---------------------------------------------------------------------------------------------
# The pins in the workflow files: read one, replace one. Nothing else in a file changes.
# ---------------------------------------------------------------------------------------------


def _single(text: str, pattern: re.Pattern[str], what: str, example: str) -> re.Match[str]:
    matches = list(pattern.finditer(text))
    if len(matches) != 1:
        raise PinError(f"{what}: expected one line like `{example}`, found {len(matches)}")
    return matches[0]


def _replace(text: str, match: re.Match[str], new: str) -> str:
    return text[: match.start("value")] + new + text[match.end("value") :]


_OSS_CAD_STEP = re.compile(r"^\s*(?:-\s+)?uses:\s*YosysHQ/setup-oss-cad-suite@\S+\s*$")
_OSS_CAD_VERSION = re.compile(
    rf'^\s+version:\s*"(?P<value>{OSS_CAD_TAG.pattern})"[ \t\r]*$', re.MULTILINE
)
_OSS_CAD_EXAMPLE = 'version: "2026-09-15"'


def _oss_cad_step(text: str) -> tuple[int, int]:
    """The start and end offsets of the text of the setup-oss-cad-suite step."""
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if _OSS_CAD_STEP.match(line.rstrip("\r\n"))]
    if len(starts) != 1:
        raise PinError(f"expected one setup-oss-cad-suite step, found {len(starts)}")
    first = starts[0]
    key_column = lines[first].index("uses:")
    end = first + 1
    while end < len(lines):
        line = lines[end]
        if line.strip() and len(line) - len(line.lstrip()) < key_column:
            break  # the next step, or the end of the steps
        end += 1
    offset = sum(len(line) for line in lines[:first])
    return offset, offset + sum(len(line) for line in lines[first:end])


def read_oss_cad(text: str) -> str:
    start, end = _oss_cad_step(text)
    match = _single(text[start:end], _OSS_CAD_VERSION, "OSS CAD Suite", _OSS_CAD_EXAMPLE)
    return match.group("value")


def write_oss_cad(text: str, version: str) -> str:
    start, end = _oss_cad_step(text)
    step = text[start:end]
    match = _single(step, _OSS_CAD_VERSION, "OSS CAD Suite", _OSS_CAD_EXAMPLE)
    return text[:start] + _replace(step, match, version) + text[end:]


_BSC_VERSION = re.compile(
    rf'^\s+BSC_VERSION:\s*"(?P<value>{BSC_TAG.pattern})"[ \t\r]*$', re.MULTILINE
)
_BSC_SHA256 = re.compile(rf'^\s+BSC_SHA256:\s*"(?P<value>{SHA256.pattern})"[ \t\r]*$', re.MULTILINE)
_BSC_VERSION_EXAMPLE = 'BSC_VERSION: "2026.07.1"'
_BSC_SHA256_EXAMPLE = 'BSC_SHA256: "<64 hexadecimal digits, lower case>"'


def read_bsc(text: str) -> tuple[str, str]:
    version = _single(text, _BSC_VERSION, "BSC_VERSION", _BSC_VERSION_EXAMPLE).group("value")
    return version, _single(text, _BSC_SHA256, "BSC_SHA256", _BSC_SHA256_EXAMPLE).group("value")


def write_bsc(text: str, version: str, sha256: str) -> str:
    # the later line first, so that the offsets of the earlier one stay valid
    for pattern, value, what, example in (
        (_BSC_SHA256, sha256, "BSC_SHA256", _BSC_SHA256_EXAMPLE),
        (_BSC_VERSION, version, "BSC_VERSION", _BSC_VERSION_EXAMPLE),
    ):
        text = _replace(text, _single(text, pattern, what, example), value)
    return text


_INSTALLER_REV = re.compile(
    rf'^\s+INSTALLER_REV:\s*"?(?P<value>{COMMIT.pattern})"?[ \t\r]*$', re.MULTILINE
)
_INSTALLER_REV_EXAMPLE = "INSTALLER_REV: <40 hexadecimal digits, lower case>"


def read_installer(text: str) -> str:
    return _single(text, _INSTALLER_REV, "INSTALLER_REV", _INSTALLER_REV_EXAMPLE).group("value")


def write_installer(text: str, commit: str) -> str:
    match = _single(text, _INSTALLER_REV, "INSTALLER_REV", _INSTALLER_REV_EXAMPLE)
    return _replace(text, match, commit)


# ---------------------------------------------------------------------------------------------
# The newest upstream. GitHub's answers are checked before they are used.
# ---------------------------------------------------------------------------------------------


def _releases(answer: Any, what: str) -> list[dict[str, Any]]:
    if not isinstance(answer, list) or not all(isinstance(item, dict) for item in answer):
        raise PinError(f"{what}: GitHub did not answer with a list of releases")
    return [item for item in answer if not item.get("draft") and not item.get("prerelease")]


def _asset_names(release: dict[str, Any]) -> set[str]:
    assets = release.get("assets")
    assets = assets if isinstance(assets, list) else []
    return {a["name"] for a in assets if isinstance(a, dict) and isinstance(a.get("name"), str)}


def newest_oss_cad(answer: Any) -> str:
    """The date of the newest build that has the Linux archive `setup-oss-cad-suite` downloads."""
    tags = [
        tag
        for release in _releases(answer, "OSS CAD Suite")
        if isinstance(tag := release.get("tag_name"), str)
        and OSS_CAD_TAG.fullmatch(tag)
        and f"oss-cad-suite-linux-x64-{tag.replace('-', '')}.tgz" in _asset_names(release)
    ]
    if not tags:
        raise PinError("OSS CAD Suite: no release has a Linux x64 archive")
    return max(tags)  # dates in this format sort as text


def _release_number(tag: str) -> tuple[int, ...]:
    return tuple(int(part) for part in tag.split("."))


def bsc_tarball(tag: str) -> str:
    return f"bsc-{tag}-ubuntu-24.04.tar.gz"


def bsc_url(tag: str) -> str:
    """The address CI downloads. It is built the way ci.yml builds it."""
    return f"{GITHUB}/{BSC_REPOSITORY}/releases/download/{tag}/{bsc_tarball(tag)}"


def newest_bsc(answer: Any) -> str:
    """The newest release that has the Ubuntu 24.04 tarball."""
    tags = [
        tag
        for release in _releases(answer, "bsc")
        if isinstance(tag := release.get("tag_name"), str)
        and BSC_TAG.fullmatch(tag)
        and bsc_tarball(tag) in _asset_names(release)
    ]
    if not tags:
        raise PinError("bsc: no release has an Ubuntu 24.04 tarball")
    return max(tags, key=_release_number)


def installer_head(answer: Any) -> str:
    sha = answer.get("sha") if isinstance(answer, dict) else None
    if not isinstance(sha, str) or not COMMIT.fullmatch(sha):
        raise PinError("openXC7 installer: GitHub did not answer with a commit")
    return sha


def installer_is_ahead(answer: Any) -> bool:
    """Whether GitHub's comparison of the pinned commit with the head says the head is ahead."""
    status = answer.get("status") if isinstance(answer, dict) else None
    if status not in ("ahead", "behind", "identical", "diverged"):
        raise PinError("openXC7 installer: GitHub did not answer with a comparison")
    return status == "ahead"


def _compare(repository: str, old: str, new: str) -> str:
    return f"{GITHUB}/{repository}/compare/{old}...{new}"


def find_update(pin: Pin, text: str, fetch: Fetch, digest: Digest) -> Update | None:
    """The update for `pin`, or None when `text` already pins the newest upstream."""
    if pin.kind == "oss_cad":
        old = read_oss_cad(text)
        new = newest_oss_cad(fetch(f"/repos/{OSS_CAD_REPOSITORY}/releases?per_page=30"))
        if new <= old:
            return None
        links = (
            ("release", f"{GITHUB}/{OSS_CAD_REPOSITORY}/releases/tag/{new}"),
            ("compare", _compare(OSS_CAD_REPOSITORY, old, new)),
        )
        return Update(pin, old, new, links)
    if pin.kind == "bsc":
        old, _ = read_bsc(text)
        new = newest_bsc(fetch(f"/repos/{BSC_REPOSITORY}/releases?per_page=30"))
        if _release_number(new) <= _release_number(old):
            return None
        sha256 = digest(bsc_url(new))
        if not SHA256.fullmatch(sha256):
            raise PinError("bsc: the digest of the tarball is not a SHA-256")
        links = (
            ("release", f"{GITHUB}/{BSC_REPOSITORY}/releases/tag/{new}"),
            ("compare", _compare(BSC_REPOSITORY, old, new)),
        )
        return Update(pin, old, new, links, sha256)
    if pin.kind == "installer":
        old = read_installer(text)
        new = installer_head(fetch(f"/repos/{INSTALLER_REPOSITORY}/commits/main"))
        if new == old:
            return None
        if not installer_is_ahead(fetch(f"/repos/{INSTALLER_REPOSITORY}/compare/{old}...{new}")):
            return None  # the pin is not behind main: it never moves back, or sideways
        return Update(pin, old, new, (("compare", _compare(INSTALLER_REPOSITORY, old, new)),))
    raise PinError(f"unknown kind of pin: {pin.kind}")


def apply_update(text: str, update: Update) -> str:
    kind = update.pin.kind
    if kind == "oss_cad":
        return write_oss_cad(text, update.new)
    if kind == "bsc":
        return write_bsc(text, update.new, update.sha256)
    if kind == "installer":
        return write_installer(text, update.new)
    raise PinError(f"unknown kind of pin: {kind}")


# ---------------------------------------------------------------------------------------------
# The text of the commit and of the pull request.
# ---------------------------------------------------------------------------------------------


def _shown(value: str) -> str:
    return value[:7] if COMMIT.fullmatch(value) else value


def commit_message(updates: Sequence[Update]) -> str:
    lines = [TITLE, ""]
    lines += [f"{u.pin.tool}: {_shown(u.old)} -> {_shown(u.new)}" for u in updates]
    return "\n".join(lines) + "\n"


def pull_request_text(updates: Sequence[Update]) -> tuple[str, str]:
    """The title and the body of the pull request, in Markdown."""
    rows = [
        f"| {u.pin.tool} | {u.pin.name} in `{u.pin.path.name}` | `{_shown(u.old)}` "
        f"| `{_shown(u.new)}` | " + ", ".join(f"[{label}]({url})" for label, url in u.links) + " |"
        for u in updates
    ]
    notes = []
    for u in updates:
        if u.pin.kind == "bsc":
            notes.append(
                f"The SHA-256 of `{bsc_tarball(u.new)}` is `{u.sha256}`. "
                "The workflow computed it by downloading the file."
            )
        if u.pin.kind == "installer":
            notes.append(
                "A new installer commit can build another yosys, nextpnr or Project X-Ray "
                "database, and `tests/test_openxc7_real.py` pins counts that depend on them. "
                "Read the changes of the installer first."
            )
    body = [
        "This pull request updates tool versions that CI pins.",
        f"The workflow `{WORKFLOW}` opens it on Mondays, when a tool has a newer version. While "
        "the pull request stays open, the workflow replaces the changes in its branch each Monday.",
        "",
        "| Tool | Pin | Old | New | Changes |",
        "| --- | --- | --- | --- | --- |",
        *rows,
        "",
        *[f"{note}\n" for note in notes],
        "CI starts on this pull request by itself. Read the changes of each tool, and merge the "
        "pull request only when CI passes. A newer tool can change a result that a test pins.",
    ]
    return TITLE, "\n".join(body) + "\n"


# ---------------------------------------------------------------------------------------------
# GitHub.
# ---------------------------------------------------------------------------------------------


def github_fetcher(token: str | None) -> Fetch:
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "xeda-bump-ci-pins",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def fetch(path: str) -> Any:
        request = urllib.request.Request(f"https://api.github.com{path}", headers=headers)
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)

    return fetch


def download_digest(url: str) -> str:
    digest = hashlib.sha256()
    size = 0
    request = urllib.request.Request(url, headers={"User-Agent": "xeda-bump-ci-pins"})
    with urllib.request.urlopen(request, timeout=120) as response:
        for chunk in iter(lambda: response.read(1 << 20), b""):
            digest.update(chunk)
            size += len(chunk)
    if not size:
        raise PinError(f"{url} is empty")
    return digest.hexdigest()


# ---------------------------------------------------------------------------------------------
# The command.
# ---------------------------------------------------------------------------------------------


def run(
    root: Path, fetch: Fetch, digest: Digest, dry_run: bool, log: Callable[[str], None]
) -> list[Update]:
    """Find the updates, and write them into the files under `root` unless `dry_run`."""
    updates: list[Update] = []
    texts: dict[Path, str] = {}
    for pin in PINS:
        label = f"{pin.tool} ({pin.path}, {pin.name})"
        path = root / pin.path
        if not path.is_file():
            log(f"{label}: skipped, the file does not exist")
            continue
        if pin.path not in texts:
            texts[pin.path] = path.read_text(encoding="utf-8")
        update = find_update(pin, texts[pin.path], fetch, digest)
        if update is None:
            log(f"{label}: up to date")
            continue
        log(f"{label}: {_shown(update.old)} -> {_shown(update.new)}")
        for name, url in update.links:
            log(f"    {name}: {url}")
        if update.sha256:
            log(f"    SHA-256 of the tarball: {update.sha256}")
        updates.append(update)
    for update in updates:
        texts[update.pin.path] = apply_update(texts[update.pin.path], update)
    if not dry_run:
        for relative in sorted({u.pin.path for u in updates}):
            (root / relative).write_text(texts[relative], encoding="utf-8")
    return updates


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="the repository (default: .)")
    parser.add_argument("--dry-run", action="store_true", help="print the changes, write nothing")
    parser.add_argument("--body-file", type=Path, help="write the text of the pull request here")
    parser.add_argument("--message-file", type=Path, help="write the commit message here")
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    try:
        updates = run(args.root, github_fetcher(token), download_digest, args.dry_run, print)
    except (PinError, OSError, ValueError) as error:  # OSError includes URLError
        print(f"error: {error}", file=sys.stderr)
        return 1

    if updates:
        title, body = pull_request_text(updates)
        if args.dry_run:
            print(f"\nThe pull request would be titled {title!r}, with this text:\n\n{body}")
        else:
            if args.body_file:
                args.body_file.write_text(body, encoding="utf-8")
            if args.message_file:
                args.message_file.write_text(commit_message(updates), encoding="utf-8")
            print(f"\nWrote {len(updates)} change(s).")
    else:
        print("\nNothing to update.")
    if output := os.environ.get("GITHUB_OUTPUT"):
        with open(output, "a", encoding="utf-8") as stream:
            stream.write(f"changed={'true' if updates else 'false'}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
