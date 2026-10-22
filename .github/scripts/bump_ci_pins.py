#!/usr/bin/env python3
"""Find newer versions of the tools that CI pins, and write them into the workflow files.

CI pins six tools. A pin is one line of a workflow file (two lines for bsc and for the image).
bsc is pinned twice, in two files, and one release moves both pins:

* the OSS CAD Suite, by the date of its build: the `version` of the
  `YosysHQ/setup-oss-cad-suite` step in `.github/workflows/ci.yml`;
* NVC, by its release number: the `version` of the `nickg/setup-nvc` step in
  `.github/workflows/ci.yml`;
* the Bluespec Compiler (bsc), by its release and the SHA-256 of its tarball: `BSC_VERSION` and
  `BSC_SHA256` in `.github/workflows/ci.yml` (the Ubuntu 24.04 tarball) and in
  `.github/workflows/openroad.yml` (the Ubuntu 22.04 tarball, for the image of the OpenROAD job);
* the openXC7 toolchain installer, by a commit: `INSTALLER_REV` in
  `.github/workflows/openxc7.yml`;
* the OpenROAD-flow-scripts (ORFS) Docker image that runs the OpenROAD tests, by its tag
  (`<yy>Q<q>-<n>-g<commit>`, as a comment) and the digest of that tag: the `image` of the
  container in `.github/workflows/openroad.yml`;
* uv, which installs Python for that job, by its release: the `version` of the
  `astral-sh/setup-uv` step in `.github/workflows/openroad.yml`. (The action itself stays at the
  version the workflow names.)

For each pin, the script asks GitHub (Docker Hub for the image) for the newest upstream and
replaces the pin when upstream is newer. It never moves a pin back. A workflow file that does not
exist has no pin to update, and the script skips it. A file that exists without its pin, or with
the pin in another format, is an error: the script must change together with the format. Every
value that comes from GitHub or Docker Hub is checked against a strict pattern before it reaches a
file. The SHA-256 of the bsc tarball comes from a download that must be whole: it has the size and
the digest that GitHub publishes for the file. A release with no published digest is not pinned:
the script says so, and a person pins it by hand. A release is pinned only when it has the
tarball of every Ubuntu version, and a digest for each: both pins stay on the same release.

The tags of the image are ordered by the numbers in them, not as text. A tag with no amd64 Linux
image is not pinned. The pin is the digest of the tag, which is what `docker pull` records. The
PDK files that the same job installs have no pin here: `providers.orfs.version` in the platform
file sets their version, and only a change of that file moves it.

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
ORFS = Path(".github/workflows/openroad.yml")
WORKFLOW = "bump-ci-pins.yml"
TITLE = "Update the pinned CI tools"

# The ASCII digits only: `\d` matches the decimal digits of every script, and a tag written with
# them would sort above every date. (The patterns are embedded in others by `.pattern`, so the
# `re.ASCII` flag would not carry over.)
OSS_CAD_TAG = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}")
BSC_TAG = re.compile(r"[0-9]{4}\.[0-9]{2}(?:\.[0-9]+)?")
# The release number of uv, as the tag of its release: no `v`, no release candidate.
UV_TAG = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")
# The release number of NVC, as the `version` of the action writes it. The tag of the release
# has an `r` before it.
NVC_NUMBER = UV_TAG
NVC_TAG = re.compile(rf"r(?P<number>{NVC_NUMBER.pattern})")
SHA256 = re.compile(r"[0-9a-f]{64}")
COMMIT = re.compile(r"[0-9a-f]{40}")
# A tag of the ORFS image: the year, the quarter, the number of commits since the start of the
# quarter and the abbreviated commit of OpenROAD-flow-scripts, as `git describe` writes them.
ORFS_TAG = re.compile(r"[0-9]{2}Q[1-4]-[0-9]+-g[0-9a-f]{7,40}")
LISTED_DIGEST = re.compile(rf"sha256:(?P<hex>{SHA256.pattern})")  # how GitHub writes a digest

GITHUB = "https://github.com"
OSS_CAD_REPOSITORY = "YosysHQ/oss-cad-suite-build"
BSC_REPOSITORY = "B-Lang-org/bsc"
#: The Ubuntu versions whose tarball a workflow installs. A release is pinned only when it has all.
BSC_PLATFORMS = ("ubuntu-24.04", "ubuntu-22.04")
INSTALLER_REPOSITORY = "openXC7/toolchain-installer"
NVC_REPOSITORY = "nickg/nvc"
#: The Ubuntu version whose package `setup-nvc` installs on the runner of the job. The action
#: picks the package whose name ends with `ubuntu-<version>.deb`.
NVC_PACKAGE_SUFFIX = "ubuntu-24.04.deb"
UV_REPOSITORY = "astral-sh/uv"
ORFS_REPOSITORY = "The-OpenROAD-Project/OpenROAD-flow-scripts"
ORFS_IMAGE = "openroad/orfs"
DOCKER_HUB = "https://hub.docker.com"
# The newest tags first. One page of 100 holds every tag of the last weeks.
ORFS_TAGS_URL = (
    f"{DOCKER_HUB}/v2/repositories/{ORFS_IMAGE}/tags?page_size=100&ordering=last_updated"
)

#: Asks GitHub's REST API for a path (`/repos/...`), or Docker Hub for an address
#: (`https://hub.docker.com/...`), and returns the parsed JSON.
Fetch = Callable[[str], Any]
#: Downloads a URL, checks the content against the size and the SHA-256 that GitHub publishes for
#: the file, and returns the SHA-256 of the content, in hexadecimal.
Digest = Callable[[str, int, str], str]


class PinError(Exception):
    """A pin cannot be read, replaced or checked."""


@dataclass(frozen=True)
class Pin:
    """One tool that CI pins, and where."""

    kind: str
    tool: str
    path: Path
    name: str
    platform: str = ""  # of the bsc tarball


PINS = (
    Pin("oss_cad", "OSS CAD Suite", CI, "`version` of the setup-oss-cad-suite step"),
    Pin("nvc", "NVC", CI, "`version` of the setup-nvc step"),
    Pin("bsc", "Bluespec Compiler (bsc)", CI, "`BSC_VERSION` and `BSC_SHA256`", "ubuntu-24.04"),
    Pin("installer", "openXC7 toolchain installer", OPENXC7, "`INSTALLER_REV`"),
    Pin("orfs", "ORFS image for the OpenROAD tests", ORFS, "`image` of the container"),
    Pin("uv", "uv", ORFS, "`version` of the setup-uv step"),
    Pin(
        "bsc",
        "Bluespec Compiler (bsc) for the OpenROAD tests",
        ORFS,
        "`BSC_VERSION` and `BSC_SHA256`",
        "ubuntu-22.04",
    ),
)


@dataclass(frozen=True)
class Update:
    """A pin that upstream has left behind."""

    pin: Pin
    old: str
    new: str
    links: tuple[tuple[str, str], ...]
    sha256: str = ""  # of the bsc tarball, or of the ORFS image (the digest of its tag)


@dataclass(frozen=True)
class Tarball:
    """The bsc tarball of a release, as GitHub's list of releases describes the file."""

    tag: str
    size: int
    sha256: str | None = None  # the digest GitHub publishes for the file, if it does


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


def _action_step(text: str, uses: re.Pattern[str], action: str) -> tuple[int, int]:
    """The start and end offsets of the text of the one step whose `uses:` line matches `uses`."""
    lines = text.splitlines(keepends=True)
    starts = [i for i, line in enumerate(lines) if uses.match(line.rstrip("\r\n"))]
    if len(starts) != 1:
        raise PinError(f"expected one {action} step, found {len(starts)}")
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


def _oss_cad_step(text: str) -> tuple[int, int]:
    return _action_step(text, _OSS_CAD_STEP, "setup-oss-cad-suite")


def read_oss_cad(text: str) -> str:
    start, end = _oss_cad_step(text)
    match = _single(text[start:end], _OSS_CAD_VERSION, "OSS CAD Suite", _OSS_CAD_EXAMPLE)
    return match.group("value")


def write_oss_cad(text: str, version: str) -> str:
    start, end = _oss_cad_step(text)
    step = text[start:end]
    match = _single(step, _OSS_CAD_VERSION, "OSS CAD Suite", _OSS_CAD_EXAMPLE)
    return text[:start] + _replace(step, match, version) + text[end:]


_NVC_STEP = re.compile(r"^\s*(?:-\s+)?uses:\s*nickg/setup-nvc@\S+\s*$")
_NVC_VERSION = re.compile(
    rf'^\s+version:\s*"(?P<value>{NVC_NUMBER.pattern})"[ \t\r]*$', re.MULTILINE
)
_NVC_EXAMPLE = 'version: "1.23.0"'


def read_nvc(text: str) -> str:
    start, end = _action_step(text, _NVC_STEP, "setup-nvc")
    return _single(text[start:end], _NVC_VERSION, "NVC", _NVC_EXAMPLE).group("value")


def write_nvc(text: str, version: str) -> str:
    start, end = _action_step(text, _NVC_STEP, "setup-nvc")
    step = text[start:end]
    match = _single(step, _NVC_VERSION, "NVC", _NVC_EXAMPLE)
    return text[:start] + _replace(step, match, version) + text[end:]


_UV_STEP = re.compile(r"^\s*(?:-\s+)?uses:\s*astral-sh/setup-uv@\S+\s*$")
_UV_VERSION = re.compile(rf'^\s+version:\s*"(?P<value>{UV_TAG.pattern})"[ \t\r]*$', re.MULTILINE)
_UV_EXAMPLE = 'version: "0.12.23"'


def read_uv(text: str) -> str:
    start, end = _action_step(text, _UV_STEP, "setup-uv")
    return _single(text[start:end], _UV_VERSION, "uv", _UV_EXAMPLE).group("value")


def write_uv(text: str, version: str) -> str:
    start, end = _action_step(text, _UV_STEP, "setup-uv")
    step = text[start:end]
    match = _single(step, _UV_VERSION, "uv", _UV_EXAMPLE)
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


# The image is the `image` of the container of the job. A workflow cannot take it from `env` there,
# so the line holds the digest itself, and the comment above it holds the tag.
_ORFS_IMAGE = re.compile(
    rf"^[ \t]*# {re.escape(ORFS_IMAGE)}:(?P<tag>{ORFS_TAG.pattern})[ \t\r]*\n"
    rf"[ \t]*image:[ \t]*{re.escape(ORFS_IMAGE)}@sha256:(?P<value>{SHA256.pattern})[ \t\r]*$",
    re.MULTILINE,
)
_ORFS_IMAGE_EXAMPLE = (
    f"# {ORFS_IMAGE}:26Q4-132-g6b305a712, and under it "
    f"image: {ORFS_IMAGE}@sha256:<64 hexadecimal digits, lower case>"
)


def read_orfs_image(text: str) -> tuple[str, str]:
    """The tag and the digest of the image. The tag is in the comment above the image line."""
    match = _single(text, _ORFS_IMAGE, "the ORFS image", _ORFS_IMAGE_EXAMPLE)
    return match.group("tag"), match.group("value")


def write_orfs_image(text: str, tag: str, digest: str) -> str:
    match = _single(text, _ORFS_IMAGE, "the ORFS image", _ORFS_IMAGE_EXAMPLE)
    # the later group first, so that the offsets of the earlier one stay valid
    text = _replace(text, match, digest)
    return text[: match.start("tag")] + tag + text[match.end("tag") :]


# ---------------------------------------------------------------------------------------------
# The newest upstream. The answers of GitHub and Docker Hub are checked before they are used.
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


def newest_nvc(answer: Any) -> str:
    """The number of the newest release that has the package `setup-nvc` installs on the runner."""
    numbers = [
        match.group("number")
        for release in _releases(answer, "NVC")
        if isinstance(tag := release.get("tag_name"), str)
        and (match := NVC_TAG.fullmatch(tag))
        and any(name.endswith(NVC_PACKAGE_SUFFIX) for name in _asset_names(release))
    ]
    if not numbers:
        raise PinError(f"NVC: no release has a package that ends with {NVC_PACKAGE_SUFFIX}")
    return max(numbers, key=_release_number)


def uv_archive() -> str:
    """The archive `setup-uv` downloads on the Linux runner of the job."""
    return "uv-x86_64-unknown-linux-gnu.tar.gz"


def newest_uv(answer: Any) -> str:
    """The number of the newest release that has the Linux archive `setup-uv` downloads."""
    tags = [
        tag
        for release in _releases(answer, "uv")
        if isinstance(tag := release.get("tag_name"), str)
        and UV_TAG.fullmatch(tag)
        and uv_archive() in _asset_names(release)
    ]
    if not tags:
        raise PinError("uv: no release has a Linux x64 archive")
    return max(tags, key=_release_number)


def bsc_tarball(tag: str, platform: str) -> str:
    return f"bsc-{tag}-{platform}.tar.gz"


def bsc_url(tag: str, platform: str) -> str:
    """The address CI downloads. It is built the way the workflows build it."""
    return f"{GITHUB}/{BSC_REPOSITORY}/releases/download/{tag}/{bsc_tarball(tag, platform)}"


def _tarball(release: dict[str, Any], tag: str, platform: str) -> Tarball:
    """The `platform` tarball of `release`, which has one: its size, and its digest if GitHub
    lists one."""
    name = bsc_tarball(tag, platform)
    asset = next(a for a in release["assets"] if isinstance(a, dict) and a.get("name") == name)
    size = asset.get("size")
    if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
        raise PinError(f"bsc: GitHub lists no size for {name}, or one that is not a size: {size!r}")
    listed = asset.get("digest")
    if listed is None:
        return Tarball(tag, size)
    match = LISTED_DIGEST.fullmatch(listed) if isinstance(listed, str) else None
    if match is None:
        raise PinError(
            f"bsc: GitHub lists a digest for {name} that is not `sha256:` and 64 hexadecimal "
            f"digits: {listed!r}"
        )
    return Tarball(tag, size, match.group("hex"))


def newest_bsc_tarballs(answer: Any) -> dict[str, Tarball]:
    """The tarball of each Ubuntu version, from the newest release that has all of them."""
    candidates = {
        tag: release
        for release in _releases(answer, "bsc")
        if isinstance(tag := release.get("tag_name"), str)
        and BSC_TAG.fullmatch(tag)
        and all(bsc_tarball(tag, platform) in _asset_names(release) for platform in BSC_PLATFORMS)
    }
    if not candidates:
        raise PinError(f"bsc: no release has a tarball for each of {', '.join(BSC_PLATFORMS)}")
    tag = max(candidates, key=_release_number)
    return {platform: _tarball(candidates[tag], tag, platform) for platform in BSC_PLATFORMS}


def newest_bsc(answer: Any, platform: str) -> Tarball:
    """The `platform` tarball of the newest release that has the tarballs of all platforms."""
    return newest_bsc_tarballs(answer)[platform]


def installer_head(answer: Any) -> str:
    sha = answer.get("sha") if isinstance(answer, dict) else None
    if not isinstance(sha, str) or not COMMIT.fullmatch(sha):
        raise PinError("openXC7 installer: GitHub did not answer with a commit")
    return sha


def installer_state(answer: Any) -> str:
    """The status in GitHub's comparison of the pinned commit (the base) with the head of main:
    `ahead` when the head has commits that the pin lacks, and nothing else is an update."""
    status = answer.get("status") if isinstance(answer, dict) else None
    if status not in ("ahead", "behind", "identical", "diverged"):
        raise PinError("openXC7 installer: GitHub did not answer with a comparison")
    return status


@dataclass(frozen=True)
class OrfsImage:
    """A tag of the ORFS image, and the digest that `image@sha256:` takes for it."""

    tag: str
    digest: str


def _orfs_number(tag: str) -> tuple[int, int, int]:
    """The year, the quarter and the count of a tag, as numbers: `26Q4-9` is older than
    `26Q4-100`."""
    year_quarter, count, _ = tag.split("-", 2)
    year, quarter = year_quarter.split("Q")
    return int(year), int(quarter), int(count)


def _has_amd64_image(tag: dict[str, Any]) -> bool:
    images = tag.get("images")
    return isinstance(images, list) and any(
        isinstance(image, dict)
        and image.get("architecture") == "amd64"
        and image.get("os") == "linux"
        and image.get("status") == "active"
        for image in images
    )


def newest_orfs(answer: Any) -> OrfsImage:
    """The newest tag of the form `<yy>Q<q>-<n>-g<commit>` that has an amd64 Linux image, with the
    digest that Docker Hub lists for the tag. That is the digest `docker pull` records, and it
    stands for the image of every platform of the tag. Other tags (`latest`, a name that is not
    a release) are ignored."""
    results = answer.get("results") if isinstance(answer, dict) else None
    if not isinstance(results, list) or not all(isinstance(item, dict) for item in results):
        raise PinError("ORFS image: Docker Hub did not answer with a list of tags")
    candidates = {
        name: tag
        for tag in results
        if isinstance(name := tag.get("name"), str)
        and ORFS_TAG.fullmatch(name)
        and _has_amd64_image(tag)
    }
    if not candidates:
        raise PinError("ORFS image: no tag of the form <yy>Q<q>-<n>-g<commit> has an amd64 image")
    name = max(candidates, key=_orfs_number)
    listed = candidates[name].get("digest")
    match = LISTED_DIGEST.fullmatch(listed) if isinstance(listed, str) else None
    if match is None:
        raise PinError(
            f"ORFS image: Docker Hub lists a digest for {name} that is not `sha256:` and 64 "
            f"hexadecimal digits: {listed!r}"
        )
    return OrfsImage(name, match.group("hex"))


_NOT_AHEAD = {
    "behind": "main ({new}) is behind the pinned commit ({old})",
    "diverged": "main ({new}) and the pinned commit ({old}) have diverged",
    "identical": "main ({new}) and the pinned commit ({old}) are identical",
}


def _orfs_commit(tag: str) -> str:
    return tag.rsplit("-g", 1)[1]


def _compare(repository: str, old: str, new: str) -> str:
    return f"{GITHUB}/{repository}/compare/{old}...{new}"


def _ignore(message: str) -> None:
    pass


def find_update(
    pin: Pin, text: str, fetch: Fetch, digest: Digest, warn: Callable[[str], None] = _ignore
) -> Update | None:
    """The update for `pin`, or None when `text` already pins the newest upstream or has no
    newer one to move to. `warn` hears of a pin that is left where it is for a reason that a
    person should look at."""
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
    if pin.kind == "nvc":
        old = read_nvc(text)
        new = newest_nvc(fetch(f"/repos/{NVC_REPOSITORY}/releases?per_page=30"))
        if _release_number(new) <= _release_number(old):
            return None
        links = (
            ("release", f"{GITHUB}/{NVC_REPOSITORY}/releases/tag/r{new}"),
            ("compare", _compare(NVC_REPOSITORY, f"r{old}", f"r{new}")),
        )
        return Update(pin, old, new, links)
    if pin.kind == "bsc":
        old, _ = read_bsc(text)
        tarballs = newest_bsc_tarballs(fetch(f"/repos/{BSC_REPOSITORY}/releases?per_page=30"))
        tarball = tarballs[pin.platform]
        new = tarball.tag
        if _release_number(new) <= _release_number(old):
            return None
        if tarball.sha256 is None or any(t.sha256 is None for t in tarballs.values()):
            # What the workflow would pin is the digest of whatever it downloads, a file that could
            # have been changed after its release to one of the same size. Only a digest that
            # GitHub published can tell. One tarball without it keeps both pins on the old
            # release, so that they never differ.
            warn(f"{new} has no published digest; pin it by hand (the pin stays at {old})")
            return None
        sha256 = digest(bsc_url(new, pin.platform), tarball.size, tarball.sha256)
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
        state = installer_state(fetch(f"/repos/{INSTALLER_REPOSITORY}/compare/{old}...{new}"))
        if state != "ahead":
            # The pin never moves back, or sideways. When main was rewritten, nothing will move
            # it until a person does, so the run says so.
            reason = _NOT_AHEAD[state].format(old=_shown(old), new=_shown(new))
            warn(f"{reason}, so the pin stays. If upstream rewrote main, move the pin by hand.")
            return None
        return Update(pin, old, new, (("compare", _compare(INSTALLER_REPOSITORY, old, new)),))
    if pin.kind == "uv":
        old = read_uv(text)
        new = newest_uv(fetch(f"/repos/{UV_REPOSITORY}/releases?per_page=30"))
        if _release_number(new) <= _release_number(old):
            return None
        links = (
            ("release", f"{GITHUB}/{UV_REPOSITORY}/releases/tag/{new}"),
            ("compare", _compare(UV_REPOSITORY, old, new)),
        )
        return Update(pin, old, new, links)
    if pin.kind == "orfs":
        old, _ = read_orfs_image(text)
        image = newest_orfs(fetch(ORFS_TAGS_URL))
        if _orfs_number(image.tag) <= _orfs_number(old):
            return None  # the same tag, or an older one: the pin never moves back
        links = (
            ("image", f"{DOCKER_HUB}/r/{ORFS_IMAGE}/tags?name={image.tag}"),
            ("compare", _compare(ORFS_REPOSITORY, _orfs_commit(old), _orfs_commit(image.tag))),
        )
        return Update(pin, old, image.tag, links, image.digest)
    raise PinError(f"unknown kind of pin: {pin.kind}")


def apply_update(text: str, update: Update) -> str:
    kind = update.pin.kind
    if kind == "oss_cad":
        return write_oss_cad(text, update.new)
    if kind == "nvc":
        return write_nvc(text, update.new)
    if kind == "bsc":
        return write_bsc(text, update.new, update.sha256)
    if kind == "installer":
        return write_installer(text, update.new)
    if kind == "uv":
        return write_uv(text, update.new)
    if kind == "orfs":
        return write_orfs_image(text, update.new, update.sha256)
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


def pull_request_text(updates: Sequence[Update], notes: Sequence[str] = ()) -> tuple[str, str]:
    """The title and the body of the pull request, in Markdown. `notes` say which pins stayed
    where they are, and why."""
    rows = [
        f"| {u.pin.tool} | {u.pin.name} in `{u.pin.path.name}` | `{_shown(u.old)}` "
        f"| `{_shown(u.new)}` | " + ", ".join(f"[{label}]({url})" for label, url in u.links) + " |"
        for u in updates
    ]
    explanations = []
    for u in updates:
        if u.pin.kind == "bsc":
            explanations.append(
                f"The SHA-256 of `{bsc_tarball(u.new, u.pin.platform)}` is `{u.sha256}`. "
                "The workflow computed it by downloading the file. "
                "GitHub lists the same digest for the file."
            )
        if u.pin.kind == "orfs":
            explanations.append(
                f"The image `{ORFS_IMAGE}:{u.new}` is pinned by its digest `sha256:{u.sha256}`, "
                "the digest that Docker Hub lists for the tag. Docker Hub lists an amd64 image "
                "for it. The OpenROAD tests run in this image against the PDK files that the "
                "platform file pins (`providers.orfs.version`). This pull request does not "
                "change that pin. Read the changes of OpenROAD-flow-scripts first."
            )
        if u.pin.kind == "installer":
            explanations.append(
                "A new installer commit can build another yosys, nextpnr or Project X-Ray "
                "database, and `tests/test_openxc7_real.py` pins counts that depend on them. "
                "Read the changes of the installer first."
            )
    stayed = ["These pins stay where they are:", "", *[f"- {note}" for note in notes], ""]
    body = [
        "This pull request updates tool versions that CI pins.",
        f"The workflow `{WORKFLOW}` opens it on Mondays, when a tool has a newer version. While "
        "the pull request stays open, the workflow replaces the changes in its branch each Monday.",
        "",
        "| Tool | Pin | Old | New | Changes |",
        "| --- | --- | --- | --- | --- |",
        *rows,
        "",
        *[f"{explanation}\n" for explanation in explanations],
        *(stayed if notes else []),
        "CI starts on this pull request by itself. Read the changes of each tool, and merge the "
        "pull request only when CI passes. A newer tool can change a result that a test pins.",
    ]
    return TITLE, "\n".join(body) + "\n"


# ---------------------------------------------------------------------------------------------
# GitHub.
# ---------------------------------------------------------------------------------------------


def upstream_fetcher(token: str | None) -> Fetch:
    """The lookups of GitHub (a path, with the token) and of Docker Hub (an address on
    `hub.docker.com`, without it: the token is for GitHub only)."""
    headers = {
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "xeda-bump-ci-pins",
    }
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def fetch(path: str) -> Any:
        if path.startswith("/"):
            request = urllib.request.Request(f"https://api.github.com{path}", headers=headers)
        elif path.startswith(f"{DOCKER_HUB}/"):
            request = urllib.request.Request(
                path, headers={k: v for k, v in headers.items() if k != "Authorization"}
            )
        else:
            raise ValueError(f"not an address of GitHub or Docker Hub: {path!r}")
        with urllib.request.urlopen(request, timeout=60) as response:
            return json.load(response)

    return fetch


def download_digest(url: str, size: int, listed: str) -> str:
    """Download `url` and return the SHA-256 of what arrived, in hexadecimal. The body must be
    whole. `read` returns `b""` for a connection that the server closed early, as it does at the
    end of a whole body, so the body must have the `size` that GitHub lists for the file, the
    length that the server announces, and the digest `listed`, which GitHub publishes."""
    digest = hashlib.sha256()
    read = 0
    request = urllib.request.Request(url, headers={"User-Agent": "xeda-bump-ci-pins"})
    with urllib.request.urlopen(request, timeout=120) as response:
        announced = response.headers.get("Content-Length")
        for chunk in iter(lambda: response.read(1 << 20), b""):
            digest.update(chunk)
            read += len(chunk)
    if not read:
        raise PinError(f"{url} is empty")
    if read != size:
        raise PinError(f"{url}: {read} bytes arrived, and GitHub lists the file as {size} bytes")
    if announced is not None:
        if not re.fullmatch(r"[0-9]+", announced):
            raise PinError(f"{url}: the server announced a Content-Length of {announced!r}")
        if read != int(announced):
            raise PinError(f"{url}: {read} bytes arrived, and the server announced {announced}")
    result = digest.hexdigest()
    if result != listed:
        raise PinError(f"{url}: its SHA-256 is {result}, and GitHub lists the digest {listed}")
    return result


# ---------------------------------------------------------------------------------------------
# The command.
# ---------------------------------------------------------------------------------------------


def run(
    root: Path,
    fetch: Fetch,
    digest: Digest,
    dry_run: bool,
    log: Callable[[str], None],
    warn: Callable[[str], None] | None = None,
) -> list[Update]:
    """Find the updates, and write them into the files under `root` unless `dry_run`. `warn`
    hears of a pin that stays where it is for a reason to look at (default: `log` it)."""
    if warn is None:

        def warn(message: str) -> None:
            log(f"warning: {message}")

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
        reasons: list[str] = []
        update = find_update(pin, texts[pin.path], fetch, digest, reasons.append)
        for reason in reasons:
            warn(f"{pin.tool}: {reason}")
        if update is None:
            if not reasons:
                log(f"{label}: up to date")
            continue
        log(f"{label}: {_shown(update.old)} -> {_shown(update.new)}")
        for name, url in update.links:
            log(f"    {name}: {url}")
        if update.sha256:
            what = "digest of the image" if update.pin.kind == "orfs" else "SHA-256 of the tarball"
            log(f"    {what}: {update.sha256}")
        updates.append(update)
    for update in updates:
        texts[update.pin.path] = apply_update(texts[update.pin.path], update)
    if not dry_run:
        for relative in sorted({u.pin.path for u in updates}):
            (root / relative).write_text(texts[relative], encoding="utf-8")
    return updates


def warn_in_the_log(message: str) -> None:
    """In GitHub Actions, a `::warning::` line shows on the page of the run, not only in its
    log. The messages hold no text from GitHub but a status from a fixed list and commit hashes."""
    if os.environ.get("GITHUB_ACTIONS") == "true":
        print(f"::warning title=Tool pins::{message}")
    else:
        print(f"warning: {message}")


def main(argv: Sequence[str] | None = None) -> int:
    notes: list[str] = []

    def warn_and_note(message: str) -> None:
        warn_in_the_log(message)
        notes.append(message)

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="the repository (default: .)")
    parser.add_argument("--dry-run", action="store_true", help="print the changes, write nothing")
    parser.add_argument("--body-file", type=Path, help="write the text of the pull request here")
    parser.add_argument("--message-file", type=Path, help="write the commit message here")
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    try:
        updates = run(
            args.root, upstream_fetcher(token), download_digest, args.dry_run, print, warn_and_note
        )
    except (PinError, OSError, ValueError) as error:  # OSError includes URLError
        print(f"error: {error}", file=sys.stderr)
        return 1

    if updates:
        title, body = pull_request_text(updates, notes)
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
