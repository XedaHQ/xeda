"""The script that keeps the tool pins of CI current (`.github/scripts/bump_ci_pins.py`).

A pin is a line of a workflow file, and the script finds and replaces it by pattern. These tests
settle four things. The real workflow files hold pins the script reads, so an edit that breaks a
pin line fails here and not on a Monday in CI. A replacement changes the pin and nothing else. A
pin never moves back, and nothing runs when upstream is not newer. What GitHub answers is checked
before it reaches a file.
"""

import hashlib
import io
import json
import re
import shutil
import subprocess
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from xeda.platforms import bundled_platform_names
from xeda.yaml_loader import load_yaml

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "bump_ci_pins.py"


def _load_script() -> types.ModuleType:
    """The script as a module. `.github/scripts/` is no package, and importing the file would
    write its bytecode into the checkout, so its source runs in a module of its own."""
    module = types.ModuleType("bump_ci_pins")
    module.__file__ = str(SCRIPT)
    sys.modules["bump_ci_pins"] = module  # the dataclasses of the script look their module up here
    exec(compile(SCRIPT.read_text(encoding="utf-8"), str(SCRIPT), "exec"), module.__dict__)
    return module


pins = _load_script()

#: Maps the ASCII digits to the Arabic-Indic digits, which `\\d` matches in a Python pattern.
ARABIC_INDIC = str.maketrans(
    "0123456789", "\u0660\u0661\u0662\u0663\u0664\u0665\u0666\u0667\u0668\u0669"
)
OLD_SHA256 = "4e69030f3a28cb819192a3780e20fd174ff7caf30cacf1cdbf2195cce913a933"
OLD_UBUNTU_22_SHA256 = "cb6aad55786a91d1c85ab0e3609e86c086e4147367e3c623676bade0e73bbd05"
NEW_UBUNTU_22_SHA256 = "12" * 32
NEW_SHA256 = "ab" * 32
OLD_COMMIT = "efe9e07330e88434a831f2c52bee72739da0f0a5"
NEW_COMMIT = "1234567890abcdef1234567890abcdef12345678"
OLD_ORFS_TAG = "26Q4-132-g6b305a712"
OLD_ORFS_DIGEST = "63981d1ac8990a9bee8bf874c95ee2c1ee3fe7b99f57d459282ab8c31a8ed663"
NEW_ORFS_TAG = "26Q4-145-g08ddc64f8"
NEW_ORFS_DIGEST = "cd" * 32
OLD_UV = "0.12.23"
NEW_UV = "0.13.1"
OLD_NVC = "1.22.1"
NEW_NVC = "1.23.0"
OLD_XEZIM = "0.11.4"
NEW_XEZIM = "0.12.0"

CI_TEXT = f"""\
jobs:
  tox:
    steps:
      - uses: actions/checkout@v5
        with:
          version: "1999-01-01"
      - uses: YosysHQ/setup-oss-cad-suite@v4
        with:
          version: "2026-09-15"
          github-token: ${{{{ secrets.GITHUB_TOKEN }}}}
      - uses: nickg/setup-nvc@v1
        with:
          version: "{OLD_NVC}"
      - name: Install Bluespec Compiler (bsc)
        env:
          BSC_VERSION: "2026.07.1"
          BSC_SHA256: "{OLD_SHA256}"
        run: |
          curl -fsSL -o bsc.tar.gz "https://example.org/${{BSC_VERSION}}/bsc.tar.gz"
          echo "${{BSC_SHA256}}  bsc.tar.gz" | sha256sum -c -
      - name: Pin xezim
        env:
          XEZIM_VERSION: "{OLD_XEZIM}"
        run: echo "XEZIM_VERSION=${{XEZIM_VERSION}}" >> "$GITHUB_ENV"
      - name: Set up Python
        uses: actions/setup-python@v6
        with:
          version: "3.11"
"""

OPENXC7_TEXT = f"""\
jobs:
  tests:
    env:
      # The one pin.
      INSTALLER_REV: {OLD_COMMIT}
      RECIPE: "1"
"""

ORFS_TEXT = f"""\
jobs:
  tests:
    container:
      # openroad/orfs:{OLD_ORFS_TAG}
      image: openroad/orfs@sha256:{OLD_ORFS_DIGEST}
      options: --user root
    steps:
      - uses: actions/checkout@v7
        with:
          version: "1999-01-01"
      - uses: astral-sh/setup-uv@v10.3.0
        with:
          version: "{OLD_UV}"
          enable-cache: false
      - uses: some/other-action@v1
        with:
          version: "2030.1.1"
      - name: Install Bluespec Compiler (bsc)
        env:
          BSC_VERSION: "2026.07.1"
          BSC_SHA256: "{OLD_UBUNTU_22_SHA256}"
        run: |
          curl -fsSL -o bsc.tar.gz "https://example.org/${{BSC_VERSION}}/bsc.tar.gz"
"""


TARBALL_SIZE = 123_456_789


def release(tag: Any, *assets: Any, **flags: bool) -> dict[str, Any]:
    """A release with assets: each a name, or a dict as GitHub describes an asset."""
    return {
        "tag_name": tag,
        "assets": [{"name": a} if isinstance(a, str) else a for a in assets],
        **flags,
    }


def oss_cad(date: str, **flags: bool) -> dict[str, Any]:
    return release(date, f"oss-cad-suite-linux-x64-{date.replace('-', '')}.tgz", **flags)


def bsc(
    tag: str,
    asset: dict[str, Any] | None = None,
    asset_22: dict[str, Any] | None = None,
    **flags: bool,
) -> dict[str, Any]:
    """A bsc release. `asset` adds to, or replaces, the fields of its Ubuntu 24.04 and 22.04
    tarballs, which have the digest that GitHub publishes unless `asset` says `{"digest": None}`.
    `asset_22` changes the Ubuntu 22.04 tarball only."""
    tarballs = [
        {
            "name": f"bsc-{tag}-{platform}.tar.gz",
            "size": TARBALL_SIZE,
            "digest": f"sha256:{NEW_SHA256}",
            **(asset or {}),
            **(asset_22 or {} if platform == "ubuntu-22.04" else {}),
        }
        for platform in ("ubuntu-24.04", "ubuntu-22.04")
    ]
    return release(tag, *tarballs, **flags)


AMD64_IMAGE = {
    "architecture": "amd64",
    "os": "linux",
    "status": "active",
    "digest": f"sha256:{'ee' * 32}",  # the manifest of the image, not the digest of the tag
}


def nvc_release(number: Any, ubuntu: str = "24.04", **flags: bool) -> dict[str, Any]:
    """An NVC release: its tag has an `r`, and `setup-nvc` installs the package of the runner's
    Ubuntu version."""
    return release(
        f"r{number}",
        f"nvc-{number}.msi",
        f"nvc-{number}.tar.gz",
        f"nvc_{number}-1_amd64_ubuntu-{ubuntu}.deb",
        **flags,
    )


def xezim_release(tag: Any, **flags: bool) -> dict[str, Any]:
    """An xezim release: it is built from source, so it has no asset."""
    return release(tag, **flags)


def uv_release(tag: Any, linux: bool = True, **flags: bool) -> dict[str, Any]:
    """A uv release: its Linux archive is what `setup-uv` downloads."""
    names = ["uv-aarch64-apple-darwin.tar.gz"]
    if linux:
        names.append("uv-x86_64-unknown-linux-gnu.tar.gz")
    return release(tag, *names, **flags)


def orfs_tag(
    name: Any, digest: Any = NEW_ORFS_DIGEST, images: list[dict[str, Any]] | None = None
) -> dict[str, Any]:
    """A tag as Docker Hub lists it: its own digest (that of the index, which `docker pull`
    records) and its images. `digest` is hexadecimal, or any other value for a bad answer."""
    listed = f"sha256:{digest}" if isinstance(digest, str) and len(digest) == 64 else digest
    return {"name": name, "digest": listed, "images": [AMD64_IMAGE] if images is None else images}


def orfs_answer(*tags: dict[str, Any]) -> dict[str, Any]:
    return {"count": len(tags), "next": None, "previous": None, "results": list(tags)}


class Upstream:
    """What GitHub and Docker Hub say, and the digest of the tarball, for the functions that
    ask."""

    def __init__(
        self,
        oss_cad_releases,
        bsc_releases,
        installer_sha,
        sha256=NEW_SHA256,
        comparison="ahead",
        orfs_tags=None,
        uv_releases=None,
        nvc_releases=None,
        xezim_releases=None,
    ):
        self.answers = {
            f"/repos/{pins.NVC_REPOSITORY}/releases?per_page=30": (
                nvc_releases
                if nvc_releases is not None
                else [nvc_release(NEW_NVC), nvc_release("1.22.2"), nvc_release(OLD_NVC)]
            ),
            f"/repos/{pins.XEZIM_REPOSITORY}/releases?per_page=30": (
                xezim_releases
                if xezim_releases is not None
                else [xezim_release(NEW_XEZIM), xezim_release(OLD_XEZIM)]
            ),
            f"/repos/{pins.UV_REPOSITORY}/releases?per_page=30": (
                uv_releases
                if uv_releases is not None
                else [uv_release(NEW_UV), uv_release("0.13.0"), uv_release(OLD_UV)]
            ),
            pins.ORFS_TAGS_URL: (
                orfs_tags
                if orfs_tags is not None
                else orfs_answer(orfs_tag(NEW_ORFS_TAG), orfs_tag(OLD_ORFS_TAG, OLD_ORFS_DIGEST))
            ),
            f"/repos/{pins.OSS_CAD_REPOSITORY}/releases?per_page=30": oss_cad_releases,
            f"/repos/{pins.BSC_REPOSITORY}/releases?per_page=30": bsc_releases,
            f"/repos/{pins.INSTALLER_REPOSITORY}/commits/main": {"sha": installer_sha},
            f"/repos/{pins.INSTALLER_REPOSITORY}/compare/{OLD_COMMIT}...{installer_sha}": {
                "status": comparison
            },
        }
        self.sha256 = sha256
        self.downloads: list[str] = []
        self.checks: list[tuple[int, str]] = []  # the size and digest each download got

    def fetch(self, path: str) -> Any:
        return self.answers[path]

    def digest(self, url: str, size: int, sha256: str) -> str:
        self.downloads.append(url)
        self.checks.append((size, sha256))
        return self.sha256


def newer_upstream(**changes) -> Upstream:
    arguments = dict(
        oss_cad_releases=[oss_cad("2026-10-07"), oss_cad("2026-10-06")],
        bsc_releases=[bsc("2026.10"), bsc("2026.07.1")],
        installer_sha=NEW_COMMIT,
    )
    return Upstream(**{**arguments, **changes})


def current_upstream() -> Upstream:
    return Upstream(
        [oss_cad("2026-09-15")],
        [bsc("2026.07.1")],
        OLD_COMMIT,
        orfs_tags=orfs_answer(orfs_tag(OLD_ORFS_TAG, OLD_ORFS_DIGEST)),
        uv_releases=[uv_release(OLD_UV)],
        nvc_releases=[nvc_release(OLD_NVC)],
        xezim_releases=[xezim_release(OLD_XEZIM)],
    )


def pin(kind: str):
    return next(p for p in pins.PINS if p.kind == kind)


def orfs_bsc_pin():
    """The pin of bsc in openroad.yml: the second pin of kind bsc."""
    return next(p for p in pins.PINS if p.kind == "bsc" and p.path == pins.ORFS)


# --------------------------------------------------------------------- the real workflow files


@pytest.mark.parametrize("pin_", pins.PINS, ids=lambda p: p.kind)
def test_the_workflow_files_of_the_repository_hold_pins_the_script_reads(pin_):
    """The pins of the files in this checkout match the patterns, and have the formats of the
    values the script writes."""
    path = ROOT / pin_.path
    if not path.is_file():
        pytest.skip(f"{pin_.path} does not exist in this checkout, so it has no pin")
    text = path.read_text(encoding="utf-8")
    if pin_.kind == "oss_cad":
        assert pins.OSS_CAD_TAG.fullmatch(pins.read_oss_cad(text))
    elif pin_.kind == "nvc":
        # a number, never `latest`: the step installs what it names, and nothing else
        assert pins.NVC_NUMBER.fullmatch(pins.read_nvc(text))
    elif pin_.kind == "xezim":
        assert pins.XEZIM_TAG.fullmatch(pins.read_xezim(text))
    elif pin_.kind == "bsc":
        version, sha256 = pins.read_bsc(text)
        assert pins.BSC_TAG.fullmatch(version) and pins.SHA256.fullmatch(sha256)
    elif pin_.kind == "installer":
        assert pins.COMMIT.fullmatch(pins.read_installer(text))
    elif pin_.kind == "orfs":
        tag, digest = pins.read_orfs_image(text)
        assert pins.ORFS_TAG.fullmatch(tag) and pins.SHA256.fullmatch(digest)
    else:
        assert pin_.kind == "uv"
        assert pins.UV_TAG.fullmatch(pins.read_uv(text))


def test_ci_downloads_the_bsc_tarball_from_the_address_the_script_hashes():
    """`bsc_url` builds the address the way ci.yml does from BSC_VERSION, so the SHA-256 that the
    script computes is the one the step checks."""
    assert pins.bsc_url("${BSC_VERSION}", "ubuntu-24.04") in (ROOT / pins.CI).read_text(
        encoding="utf-8"
    )


def test_the_openroad_job_downloads_the_bsc_tarball_of_its_own_ubuntu_version():
    """The image of the job is Ubuntu 22.04, so the pin there is the digest of that tarball."""
    text = (ROOT / pins.ORFS).read_text(encoding="utf-8")
    assert pins.bsc_url("${BSC_VERSION}", "ubuntu-22.04") in text
    assert "ubuntu-24.04" not in text.replace("runs-on: ubuntu-24.04", "")


def test_both_bsc_pins_of_the_repository_name_the_same_release():
    versions = {
        pins.read_bsc((ROOT / path).read_text(encoding="utf-8"))[0] for path in (pins.CI, pins.ORFS)
    }
    assert len(versions) == 1


# ------------------------------------------------------------------------ reading a pin


def test_each_pin_is_read():
    assert pins.read_oss_cad(CI_TEXT) == "2026-09-15"
    assert pins.read_nvc(CI_TEXT) == OLD_NVC
    assert pins.read_bsc(CI_TEXT) == ("2026.07.1", OLD_SHA256)
    assert pins.read_xezim(CI_TEXT) == OLD_XEZIM
    assert pins.read_installer(OPENXC7_TEXT) == OLD_COMMIT
    assert pins.read_orfs_image(ORFS_TEXT) == (OLD_ORFS_TAG, OLD_ORFS_DIGEST)
    assert pins.read_uv(ORFS_TEXT) == OLD_UV
    assert pins.read_bsc(ORFS_TEXT) == ("2026.07.1", OLD_UBUNTU_22_SHA256)


def test_the_oss_cad_pin_is_the_version_of_its_own_step_and_no_other():
    """The `version` of another action, before the step or after it, is not the pin."""
    assert pins.read_oss_cad(CI_TEXT) == "2026-09-15"
    own_version = '          version: "2026-09-15"\n'
    later_step = (
        '      - uses: some/other-action@v1\n        with:\n          version: "2030-01-01"\n'
    )
    with pytest.raises(pins.PinError, match="version"):
        pins.read_oss_cad(CI_TEXT.replace(own_version, "") + later_step)
    assert pins.read_oss_cad(CI_TEXT + later_step) == "2026-09-15"


def test_the_nvc_pin_is_the_version_of_its_own_step_and_no_other():
    """The `version` of another step, before or after the NVC step, is not the pin."""
    own = f'          version: "{OLD_NVC}"\n'
    later_step = (
        '      - uses: some/other-action@v1\n        with:\n          version: "2030.1.1"\n'
    )
    with pytest.raises(pins.PinError, match="version"):
        pins.read_nvc(CI_TEXT.replace(own, "") + later_step)
    assert pins.read_nvc(CI_TEXT + later_step) == OLD_NVC
    assert pins.write_nvc(CI_TEXT + later_step, NEW_NVC).count('"2030.1.1"') == 1


@pytest.mark.parametrize(
    "text, reader, message",
    [
        pytest.param(CI_TEXT.replace("setup-nvc", "setup-ghdl"), "nvc", "step", id="nvc step"),
        pytest.param(CI_TEXT.replace(f'"{OLD_NVC}"', "latest"), "nvc", "version", id="nvc latest"),
        pytest.param(
            CI_TEXT.replace(f'"{OLD_NVC}"', '"latest"'), "nvc", "version", id="nvc quoted latest"
        ),
        pytest.param(
            CI_TEXT.replace(f'"{OLD_NVC}"', OLD_NVC), "nvc", "version", id="nvc no quotes"
        ),
        pytest.param(
            CI_TEXT.replace(f'"{OLD_NVC}"', f'"r{OLD_NVC}"'), "nvc", "version", id="nvc r prefix"
        ),
        pytest.param(CI_TEXT.replace(f'"{OLD_NVC}"', '"1.22"'), "nvc", "version", id="nvc two"),
        pytest.param(
            CI_TEXT.replace(f'          version: "{OLD_NVC}"\n', ""),
            "nvc",
            "version",
            id="no nvc version",
        ),
        pytest.param(CI_TEXT + CI_TEXT, "nvc", "step", id="nvc step twice"),
        pytest.param(CI_TEXT.replace("setup-oss-cad-suite", "setup-oss"), "oss", "step", id="step"),
        pytest.param(
            CI_TEXT.replace('"2026-09-15"', "2026-09-15"), "oss", "version", id="no quotes"
        ),
        pytest.param(
            CI_TEXT.replace('"2026-09-15"', '"latest"'), "oss", "version", id="not a date"
        ),
        pytest.param(
            CI_TEXT.replace('"2026.07.1"', "'2026.07.1'"), "bsc", "BSC_VERSION", id="single quote"
        ),
        pytest.param(
            CI_TEXT.replace('"2026.07.1"', '"v2026.07"'), "bsc", "BSC_VERSION", id="v prefix"
        ),
        pytest.param(
            CI_TEXT.replace(OLD_SHA256, OLD_SHA256.upper()), "bsc", "BSC_SHA256", id="upper case"
        ),
        pytest.param(CI_TEXT + CI_TEXT, "bsc", "BSC_VERSION", id="twice"),
        pytest.param(
            OPENXC7_TEXT.replace(OLD_COMMIT, "main"), "installer", "INSTALLER_REV", id="branch"
        ),
        pytest.param(OPENXC7_TEXT + OPENXC7_TEXT, "installer", "INSTALLER_REV", id="rev twice"),
        pytest.param(
            ORFS_TEXT.replace(f"@sha256:{OLD_ORFS_DIGEST}", f":{OLD_ORFS_TAG}"),
            "orfs",
            "image",
            id="image by tag",
        ),
        pytest.param(
            ORFS_TEXT.replace(OLD_ORFS_DIGEST, OLD_ORFS_DIGEST.upper()),
            "orfs",
            "image",
            id="image digest in upper case",
        ),
        pytest.param(
            ORFS_TEXT.replace(OLD_ORFS_DIGEST, OLD_ORFS_DIGEST[:-1]), "orfs", "image", id="short"
        ),
        pytest.param(
            ORFS_TEXT.replace("openroad/orfs@", "openroad/other@"),
            "orfs",
            "image",
            id="other image",
        ),
        pytest.param(
            ORFS_TEXT.replace(f"# openroad/orfs:{OLD_ORFS_TAG}\n      ", ""),
            "orfs",
            "image",
            id="no tag",
        ),
        pytest.param(
            ORFS_TEXT.replace(OLD_ORFS_TAG, "latest"), "orfs", "image", id="tag of another form"
        ),
        pytest.param(ORFS_TEXT + ORFS_TEXT, "orfs", "image", id="image twice"),
        pytest.param(ORFS_TEXT.replace("setup-uv", "setup-pdm"), "uv", "step", id="uv step"),
        pytest.param(ORFS_TEXT.replace(f'"{OLD_UV}"', OLD_UV), "uv", "version", id="uv no quotes"),
        pytest.param(
            ORFS_TEXT.replace(f'"{OLD_UV}"', '"latest"'), "uv", "version", id="uv not a version"
        ),
        pytest.param(
            ORFS_TEXT.replace(f'"{OLD_UV}"', '"0.12"'), "uv", "version", id="uv two numbers"
        ),
        pytest.param(
            ORFS_TEXT.replace(f'"{OLD_UV}"', f'"{OLD_UV}rc1"'), "uv", "version", id="uv rc"
        ),
        pytest.param(
            ORFS_TEXT.replace(f'"{OLD_UV}"', f'"{OLD_UV}"'.translate(ARABIC_INDIC)),
            "uv",
            "version",
            id="uv other digits",
        ),
        pytest.param(
            ORFS_TEXT.replace(f'          version: "{OLD_UV}"\n', ""),
            "uv",
            "version",
            id="no uv version",
        ),
        pytest.param(ORFS_TEXT + ORFS_TEXT, "uv", "step", id="uv step twice"),
        pytest.param(
            CI_TEXT.replace(f'"{OLD_XEZIM}"', "latest"), "xezim", "XEZIM", id="xezim latest"
        ),
        pytest.param(
            CI_TEXT.replace(f'"{OLD_XEZIM}"', f'"v{OLD_XEZIM}"'), "xezim", "XEZIM", id="xezim v"
        ),
        pytest.param(CI_TEXT.replace(f'"{OLD_XEZIM}"', '"0.11"'), "xezim", "XEZIM", id="xezim two"),
        pytest.param(CI_TEXT + CI_TEXT, "xezim", "XEZIM", id="xezim twice"),
        pytest.param(
            CI_TEXT.replace('"2026-09-15"', '"2026-09-15"'.translate(ARABIC_INDIC)),
            "oss",
            "version",
            id="other digits",
        ),
    ],
)
def test_a_pin_in_another_format_is_an_error_not_a_guess(text, reader, message):
    read = {
        "oss": pins.read_oss_cad,
        "nvc": pins.read_nvc,
        "bsc": pins.read_bsc,
        "installer": pins.read_installer,
        "orfs": pins.read_orfs_image,
        "uv": pins.read_uv,
        "xezim": pins.read_xezim,
    }
    with pytest.raises(pins.PinError, match=message):
        read[reader](text)


# --------------------------------------------------------------------- replacing a pin


def test_replacing_a_pin_changes_that_line_and_no_other():
    assert pins.write_oss_cad(CI_TEXT, "2026-10-07").replace("2026-10-07", "2026-09-15") == CI_TEXT
    rewritten = pins.write_nvc(CI_TEXT, NEW_NVC)
    assert pins.read_nvc(rewritten) == NEW_NVC
    assert rewritten.replace(NEW_NVC, OLD_NVC) == CI_TEXT
    rewritten = pins.write_xezim(CI_TEXT, NEW_XEZIM)
    assert pins.read_xezim(rewritten) == NEW_XEZIM
    assert rewritten.replace(NEW_XEZIM, OLD_XEZIM) == CI_TEXT
    rewritten = pins.write_bsc(CI_TEXT, "2026.10", NEW_SHA256)
    assert pins.read_bsc(rewritten) == ("2026.10", NEW_SHA256)
    assert rewritten.replace("2026.10", "2026.07.1").replace(NEW_SHA256, OLD_SHA256) == CI_TEXT
    rewritten = pins.write_installer(OPENXC7_TEXT, NEW_COMMIT)
    assert rewritten.replace(NEW_COMMIT, OLD_COMMIT) == OPENXC7_TEXT
    rewritten = pins.write_uv(ORFS_TEXT, NEW_UV)
    assert pins.read_uv(rewritten) == NEW_UV
    assert rewritten.replace(NEW_UV, OLD_UV) == ORFS_TEXT
    rewritten = pins.write_orfs_image(ORFS_TEXT, NEW_ORFS_TAG, NEW_ORFS_DIGEST)
    assert pins.read_orfs_image(rewritten) == (NEW_ORFS_TAG, NEW_ORFS_DIGEST)
    assert (
        rewritten.replace(NEW_ORFS_TAG, OLD_ORFS_TAG).replace(NEW_ORFS_DIGEST, OLD_ORFS_DIGEST)
        == ORFS_TEXT
    )


def test_the_uv_pin_is_the_version_of_its_own_step_and_no_other():
    """The `version` of another action, before or after the step, is not the pin."""
    own = f'          version: "{OLD_UV}"\n'
    assert pins.read_uv(ORFS_TEXT) == OLD_UV  # another step follows the pin, with its own version
    with pytest.raises(pins.PinError, match="version"):
        pins.read_uv(ORFS_TEXT.replace(own, ""))
    assert pins.write_uv(ORFS_TEXT, NEW_UV).count('"2030.1.1"') == 1


def test_replacing_a_pin_keeps_the_quotes_and_the_line_endings():
    text = OPENXC7_TEXT.replace(OLD_COMMIT, f'"{OLD_COMMIT}"').replace("\n", "\r\n")
    rewritten = pins.write_installer(text, NEW_COMMIT)
    assert rewritten == text.replace(OLD_COMMIT, NEW_COMMIT)
    assert f'INSTALLER_REV: "{NEW_COMMIT}"\r\n' in rewritten
    text = ORFS_TEXT.replace("\n", "\r\n")
    rewritten = pins.write_orfs_image(text, NEW_ORFS_TAG, NEW_ORFS_DIGEST)
    assert f"image: openroad/orfs@sha256:{NEW_ORFS_DIGEST}\r\n" in rewritten
    assert f"# openroad/orfs:{NEW_ORFS_TAG}\r\n" in rewritten


# ----------------------------------------------------------------- what GitHub answers


def test_the_newest_oss_cad_build_has_the_linux_archive_the_action_downloads():
    answer = [
        oss_cad("2026-10-09"),  # chosen below: the order of the list does not matter
        release("2026-10-10", "oss-cad-suite-darwin-arm64-20261010.tgz"),  # no Linux archive
        oss_cad("2026-10-11", draft=True),
        oss_cad("2026-10-12", prerelease=True),
        release("latest", "oss-cad-suite-linux-x64-latest.tgz"),
        oss_cad("2026-10-08"),
    ]
    assert pins.newest_oss_cad(answer) == "2026-10-09"


def test_a_tag_written_with_other_digits_is_no_release_tag():
    """`\\d` matches the decimal digits of every script, and such a tag would sort above every
    date. Both patterns take the ASCII digits only."""
    dated = "2026-10-08".translate(ARABIC_INDIC)
    assert pins.newest_oss_cad([oss_cad("2026-10-06"), oss_cad(dated)]) == "2026-10-06"
    numbered = "2026.10".translate(ARABIC_INDIC)
    assert pins.newest_bsc([bsc("2026.07.1"), bsc(numbered)], "ubuntu-24.04").tag == "2026.07.1"
    assert not pins.OSS_CAD_TAG.fullmatch(dated) and not pins.BSC_TAG.fullmatch(numbered)


@pytest.mark.parametrize("answer", [[], {"message": "rate limit"}, "text", [1, 2], [release("x")]])
def test_no_usable_oss_cad_release_is_an_error(answer):
    with pytest.raises(pins.PinError):
        pins.newest_oss_cad(answer)


def test_the_newest_nvc_release_has_the_package_the_action_installs():
    answer = [
        nvc_release("1.12.9"),
        nvc_release("1.23.2", ubuntu="22.04"),  # no package for the Ubuntu of the runner
        nvc_release("1.23.3", draft=True),
        nvc_release("1.23.4", prerelease=True),
        release("1.23.5", "nvc_1.23.5-1_amd64_ubuntu-24.04.deb"),  # a tag without the `r`
        release("r1.23.6rc1", "nvc_1.23.6rc1-1_amd64_ubuntu-24.04.deb"),  # not a release number
        nvc_release("1.22.1"),
        nvc_release("1.12.10"),
    ]
    assert pins.newest_nvc(answer) == "1.22.1"


def test_nvc_releases_are_ordered_as_numbers_not_as_text():
    assert pins.newest_nvc(
        [nvc_release("1.9.0"), nvc_release("1.12.2"), nvc_release("1.12.10")]
    ) == ("1.12.10")
    assert pins.newest_nvc([nvc_release("2.0.0"), nvc_release("1.99.99")]) == "2.0.0"


def test_an_nvc_tag_written_with_other_digits_is_no_release_tag():
    other = "r1.2.3".translate(ARABIC_INDIC)
    assert pins.newest_nvc([nvc_release("1.22.1"), nvc_release(other[1:])]) == "1.22.1"
    assert not pins.NVC_TAG.fullmatch(other)


@pytest.mark.parametrize(
    "answer",
    [[], {"message": "rate limit"}, "text", [1], [release("r1.23.0")], [nvc_release("1.2")]],
)
def test_no_usable_nvc_release_is_an_error(answer):
    with pytest.raises(pins.PinError):
        pins.newest_nvc(answer)


def test_the_newest_bsc_release_has_the_tarball_of_each_ubuntu_version():
    answer = [
        bsc("2026.01"),
        bsc("2026.07"),
        bsc("2026.07.1"),
        release("2026.12", "bsc-2026.12-ubuntu-24.04-arm.tar.gz"),  # another architecture
        release("2026.11", "bsc-2026.11-ubuntu-22.04.tar.gz"),  # no Ubuntu 24.04
        release("2026.11.1", "bsc-2026.11.1-ubuntu-24.04.tar.gz"),  # no Ubuntu 22.04
        bsc("2026.10", prerelease=True),
        bsc("2026.09", draft=True),
        bsc("2026.08-rc1"),  # not a release number
        bsc("nightly"),
    ]
    assert pins.newest_bsc(answer, "ubuntu-24.04").tag == "2026.07.1"
    assert pins.newest_bsc(answer, "ubuntu-22.04").tag == "2026.07.1"


def test_bsc_releases_are_ordered_as_numbers_not_as_text():
    assert pins.newest_bsc([bsc("2026.07"), bsc("2026.07.1")], "ubuntu-24.04").tag == "2026.07.1"
    assert (
        pins.newest_bsc([bsc("2025.12.3"), bsc("2025.12.10")], "ubuntu-22.04").tag == "2025.12.10"
    )


@pytest.mark.parametrize("answer", [[], {"message": "rate limit"}, [release("2026.01")]])
def test_no_usable_bsc_release_is_an_error(answer):
    with pytest.raises(pins.PinError):
        pins.newest_bsc(answer, "ubuntu-24.04")


def test_the_tarball_of_a_release_has_the_size_and_the_digest_github_lists_for_it():
    answer = [bsc("2026.10", {"size": 1234, "digest": f"sha256:{NEW_SHA256}"}), bsc("2026.07.1")]
    tarball = pins.newest_bsc(answer, "ubuntu-22.04")
    assert (tarball.tag, tarball.size, tarball.sha256) == ("2026.10", 1234, NEW_SHA256)


@pytest.mark.parametrize("fields", [{}, {"digest": None}], ids=["no digest", "digest null"])
def test_a_tarball_that_github_gives_no_digest_has_none(fields):
    assets = [
        {"name": f"bsc-2026.10-{platform}.tar.gz", "size": TARBALL_SIZE, **fields}
        for platform in pins.BSC_PLATFORMS
    ]
    tarball = pins.newest_bsc([release("2026.10", *assets)], "ubuntu-24.04")
    assert (tarball.size, tarball.sha256) == (TARBALL_SIZE, None)


@pytest.mark.parametrize(
    "asset",
    [
        pytest.param({"size": None}, id="no size"),
        pytest.param({"size": 0}, id="empty"),
        pytest.param({"size": -1}, id="negative"),
        pytest.param({"size": "100"}, id="text"),
        pytest.param({"size": True}, id="boolean"),
        pytest.param({"size": 1.5}, id="fraction"),
        pytest.param({"digest": "sha512:" + "ab" * 64}, id="another algorithm"),
        pytest.param({"digest": "sha256:" + "AB" * 32}, id="upper case"),
        pytest.param({"digest": "sha256:abc"}, id="short"),
        pytest.param({"digest": ""}, id="empty digest"),
        pytest.param({"digest": 7}, id="number"),
    ],
)
def test_a_tarball_that_github_describes_wrongly_is_an_error(asset):
    with pytest.raises(pins.PinError):
        pins.newest_bsc([bsc("2026.10", asset)], "ubuntu-24.04")


@pytest.mark.parametrize("answer", [{}, [], {"sha": None}, {"sha": "main"}, {"sha": "ab" * 21}])
def test_an_installer_answer_that_is_no_commit_is_an_error(answer):
    with pytest.raises(pins.PinError):
        pins.installer_head(answer)


def test_an_installer_commit_is_taken_whole():
    assert pins.installer_head({"sha": NEW_COMMIT}) == NEW_COMMIT


@pytest.mark.parametrize(
    "tag",
    ["26Q4-132-g6b305a712", "25Q1-0-g0000000", "26Q3-705-g" + "a" * 40],
)
def test_an_orfs_tag_has_a_year_a_quarter_a_count_and_a_commit(tag):
    assert pins.ORFS_TAG.fullmatch(tag)


@pytest.mark.parametrize(
    "tag",
    [
        "latest",
        "26Q4-145",
        "26Q4-145-g08ddc6",  # a short commit
        "26Q4-145-gXYZXYZXYZ",
        "26q4-145-g08ddc64f8",
        "26Q5-145-g08ddc64f8",
        "26Q0-145-g08ddc64f8",
        "2026Q4-145-g08ddc64f8",
        "26Q4-145-g08DDC64F8",
        "v26Q4-145-g08ddc64f8",
        "26Q4-145-g08ddc64f8-debug",
        "26Q4-145-g08ddc64f8\n",
        "26Q4-145-g" + "a" * 41,
        "26Q4-14\u0665-g08ddc64f8",  # an Arabic-Indic digit
        None,
        7,
    ],
)
def test_a_tag_of_another_form_is_ignored(tag):
    answer = orfs_answer(orfs_tag(tag), orfs_tag(OLD_ORFS_TAG, OLD_ORFS_DIGEST))
    assert pins.newest_orfs(answer) == pins.OrfsImage(OLD_ORFS_TAG, OLD_ORFS_DIGEST)


def test_the_newest_orfs_tag_is_chosen_by_number_not_by_text():
    """A year, a quarter and a count, as numbers: `26Q4-9` is older than `26Q4-100`, and a quarter
    of the year before is older than every quarter of this one, whatever order Docker Hub lists
    the tags in."""
    tags = ["26Q4-9-g0000009", "26Q4-100-g0000100", "26Q3-705-g0007050", "25Q4-9999-g0099990"]
    expected = pins.OrfsImage("26Q4-100-g0000100", NEW_ORFS_DIGEST)
    assert pins.newest_orfs(orfs_answer(*map(orfs_tag, tags))) == expected
    assert pins.newest_orfs(orfs_answer(*map(orfs_tag, reversed(tags)))) == expected


def test_the_orfs_digest_is_the_one_of_the_tag_not_the_one_of_its_image():
    """What `docker pull` records and `image@sha256:` takes, for the platform of the runner."""
    answer = orfs_answer(orfs_tag(NEW_ORFS_TAG, "ab" * 32))
    assert pins.newest_orfs(answer).digest == "ab" * 32


@pytest.mark.parametrize(
    "images",
    [
        pytest.param([], id="none"),
        pytest.param([{**AMD64_IMAGE, "architecture": "arm64"}], id="arm64 only"),
        pytest.param([{**AMD64_IMAGE, "os": "windows"}], id="another system"),
        pytest.param([{**AMD64_IMAGE, "status": "inactive"}], id="inactive"),
        pytest.param([{k: v for k, v in AMD64_IMAGE.items() if k != "status"}], id="no status"),
        pytest.param(["amd64"], id="not described"),
    ],
)
def test_a_tag_without_an_amd64_image_is_not_pinned(images):
    answer = orfs_answer(
        orfs_tag(NEW_ORFS_TAG, images=images), orfs_tag(OLD_ORFS_TAG, OLD_ORFS_DIGEST)
    )
    assert pins.newest_orfs(answer) == pins.OrfsImage(OLD_ORFS_TAG, OLD_ORFS_DIGEST)
    with pytest.raises(pins.PinError, match="amd64"):
        pins.newest_orfs(orfs_answer(orfs_tag(NEW_ORFS_TAG, images=images)))


@pytest.mark.parametrize(
    "answer",
    [[], {}, {"results": None}, {"results": "x"}, {"results": [1]}, {"message": "rate limit"}, "x"],
)
def test_an_orfs_answer_that_is_no_list_of_tags_is_an_error(answer):
    with pytest.raises(pins.PinError):
        pins.newest_orfs(answer)


@pytest.mark.parametrize(
    "digest",
    [
        None,
        "",
        "sha256:" + "AB" * 32,
        "sha256:abc",
        "sha512:" + "ab" * 64,
        7,
    ],
)
def test_an_orfs_tag_with_a_bad_digest_is_an_error(digest):
    with pytest.raises(pins.PinError, match="digest"):
        pins.newest_orfs(orfs_answer(orfs_tag(NEW_ORFS_TAG, digest)))


def test_the_newest_uv_release_has_the_linux_archive_the_action_downloads():
    answer = [
        uv_release("0.12.9"),
        uv_release("0.13.2", linux=False),  # no archive for Linux
        uv_release("0.13.3", draft=True),
        uv_release("0.13.4", prerelease=True),
        uv_release("0.13.5rc1"),  # not a release number
        uv_release("v0.13.6"),
        uv_release("0.13.1"),
        uv_release("0.12.24"),
    ]
    assert pins.newest_uv(answer) == "0.13.1"


def test_uv_releases_are_ordered_as_numbers_not_as_text():
    assert pins.newest_uv([uv_release("0.9.30"), uv_release("0.12.2"), uv_release("0.12.10")]) == (
        "0.12.10"
    )
    assert pins.newest_uv([uv_release("1.0.0"), uv_release("0.99.99")]) == "1.0.0"


def test_a_uv_tag_written_with_other_digits_is_no_release_tag():
    other = "0.13.1".translate(ARABIC_INDIC)
    assert pins.newest_uv([uv_release("0.12.23"), uv_release(other)]) == "0.12.23"
    assert not pins.UV_TAG.fullmatch(other)


@pytest.mark.parametrize("answer", [[], {"message": "rate limit"}, "text", [1], [release("0.1.0")]])
def test_no_usable_uv_release_is_an_error(answer):
    with pytest.raises(pins.PinError):
        pins.newest_uv(answer)


# ----------------------------------------------------------- finding and applying updates


def test_nothing_is_updated_when_upstream_is_where_the_pins_are():
    upstream = current_upstream()
    for kind, text in (
        ("oss_cad", CI_TEXT),
        ("nvc", CI_TEXT),
        ("bsc", CI_TEXT),
        ("xezim", CI_TEXT),
        ("installer", OPENXC7_TEXT),
        ("orfs", ORFS_TEXT),
    ):
        assert pins.find_update(pin(kind), text, upstream.fetch, upstream.digest) is None
    assert pins.find_update(orfs_bsc_pin(), ORFS_TEXT, upstream.fetch, upstream.digest) is None
    assert upstream.downloads == []


def test_a_pin_never_moves_back():
    upstream = Upstream(
        [oss_cad("2026-09-01")], [bsc("2026.01"), bsc("2026.07")], OLD_COMMIT, sha256="x"
    )
    for kind, text in (("oss_cad", CI_TEXT), ("bsc", CI_TEXT)):
        assert pins.find_update(pin(kind), text, upstream.fetch, upstream.digest) is None
    assert upstream.downloads == []  # an older release is not even downloaded


def test_a_newer_oss_cad_build_is_an_update_with_its_links():
    upstream = newer_upstream()
    update = pins.find_update(pin("oss_cad"), CI_TEXT, upstream.fetch, upstream.digest)
    assert (update.old, update.new) == ("2026-09-15", "2026-10-07")
    assert dict(update.links) == {
        "release": "https://github.com/YosysHQ/oss-cad-suite-build/releases/tag/2026-10-07",
        "compare": "https://github.com/YosysHQ/oss-cad-suite-build/compare/2026-09-15...2026-10-07",
    }
    assert pins.apply_update(CI_TEXT, update) == pins.write_oss_cad(CI_TEXT, "2026-10-07")


def test_a_newer_nvc_is_an_update_with_its_links():
    upstream = newer_upstream()
    update = pins.find_update(pin("nvc"), CI_TEXT, upstream.fetch, upstream.digest)
    assert (update.old, update.new) == (OLD_NVC, NEW_NVC)
    assert dict(update.links) == {
        "release": f"https://github.com/nickg/nvc/releases/tag/r{NEW_NVC}",
        "compare": f"https://github.com/nickg/nvc/compare/r{OLD_NVC}...r{NEW_NVC}",
    }
    assert pins.apply_update(CI_TEXT, update) == pins.write_nvc(CI_TEXT, NEW_NVC)


def test_the_newest_xezim_release_is_a_plain_number_and_never_moves_back():
    answer = [
        xezim_release("0.13.0", draft=True),
        xezim_release("0.12.1", prerelease=True),
        xezim_release("v0.12.2"),  # a `v`: not a tag of this project
        xezim_release("0.12.3rc1"),
        xezim_release("0.9.0"),
        xezim_release("0.11.10"),
    ]
    assert pins.newest_xezim(answer) == "0.11.10"  # ordered as numbers, not as text
    for releases in ([xezim_release("0.11.3"), xezim_release("0.9.0")], [xezim_release(OLD_XEZIM)]):
        upstream = newer_upstream(xezim_releases=releases)
        assert pins.find_update(pin("xezim"), CI_TEXT, upstream.fetch, upstream.digest) is None
    with pytest.raises(pins.PinError, match="xezim"):
        pins.newest_xezim([xezim_release("v1.0.0")])


def test_a_newer_xezim_is_an_update_with_its_links():
    upstream = newer_upstream()
    update = pins.find_update(pin("xezim"), CI_TEXT, upstream.fetch, upstream.digest)
    assert (update.old, update.new) == (OLD_XEZIM, NEW_XEZIM)
    assert (
        dict(update.links)["release"] == f"https://github.com/aionhw/xezim/releases/tag/{NEW_XEZIM}"
    )
    assert pins.apply_update(CI_TEXT, update) == pins.write_xezim(CI_TEXT, NEW_XEZIM)


def test_nvc_never_moves_back_and_a_pin_at_the_newest_is_not_an_update():
    for releases in ([nvc_release("1.22.0"), nvc_release("1.9.0")], [nvc_release(OLD_NVC)]):
        upstream = newer_upstream(nvc_releases=releases)
        assert pins.find_update(pin("nvc"), CI_TEXT, upstream.fetch, upstream.digest) is None


def test_a_newer_bsc_is_hashed_from_the_address_ci_downloads():
    upstream = newer_upstream()
    update = pins.find_update(pin("bsc"), CI_TEXT, upstream.fetch, upstream.digest)
    assert (update.old, update.new, update.sha256) == ("2026.07.1", "2026.10", NEW_SHA256)
    assert upstream.downloads == [
        "https://github.com/B-Lang-org/bsc/releases/download/2026.10/bsc-2026.10-ubuntu-24.04.tar.gz"
    ]
    assert pins.read_bsc(pins.apply_update(CI_TEXT, update)) == ("2026.10", NEW_SHA256)


def test_a_newer_bsc_is_hashed_for_the_ubuntu_version_of_each_workflow():
    upstream = newer_upstream()
    update = pins.find_update(orfs_bsc_pin(), ORFS_TEXT, upstream.fetch, upstream.digest)
    assert (update.old, update.new) == ("2026.07.1", "2026.10")
    assert upstream.downloads == [
        "https://github.com/B-Lang-org/bsc/releases/download/2026.10/bsc-2026.10-ubuntu-22.04.tar.gz"
    ]
    assert pins.read_bsc(pins.apply_update(ORFS_TEXT, update)) == ("2026.10", NEW_SHA256)
    assert pins.read_uv(pins.apply_update(ORFS_TEXT, update)) == OLD_UV  # no other pin changes


def test_a_bsc_release_without_the_digest_of_one_tarball_moves_neither_pin():
    """The pins of the two workflows never name different releases."""
    upstream = newer_upstream(
        bsc_releases=[bsc("2026.10", asset_22={"digest": None}), bsc("2026.07.1")]
    )
    for bsc_pin, text in ((pin("bsc"), CI_TEXT), (orfs_bsc_pin(), ORFS_TEXT)):
        notes: list[str] = []
        update = pins.find_update(bsc_pin, text, upstream.fetch, upstream.digest, notes.append)
        assert update is None and len(notes) == 1
    assert upstream.downloads == []


def test_a_newer_bsc_is_downloaded_against_the_size_and_digest_github_lists():
    listed = {"size": 4321, "digest": f"sha256:{NEW_SHA256}"}
    upstream = newer_upstream(bsc_releases=[bsc("2026.10", listed), bsc("2026.07.1")])
    pins.find_update(pin("bsc"), CI_TEXT, upstream.fetch, upstream.digest)
    assert upstream.checks == [(4321, NEW_SHA256)]


@pytest.mark.parametrize("asset", [{"digest": None}, {"digest": None, "size": 4321}], ids=str)
def test_a_newer_bsc_without_a_published_digest_is_not_pinned(asset):
    """The digest the workflow computes from a download is only as good as the download: a file
    changed after its release, to one of the same size, would become the pin. Without a digest
    that GitHub publishes, the pin stays and a person pins by hand."""
    upstream = newer_upstream(bsc_releases=[bsc("2026.10", asset), bsc("2026.07.1")])
    notes: list[str] = []
    update = pins.find_update(pin("bsc"), CI_TEXT, upstream.fetch, upstream.digest, notes.append)
    assert update is None
    assert upstream.downloads == []  # nothing is downloaded to take a digest from
    assert notes == ["2026.10 has no published digest; pin it by hand (the pin stays at 2026.07.1)"]


def test_a_bsc_that_is_not_newer_needs_no_digest():
    upstream = newer_upstream(bsc_releases=[bsc("2026.07.1", {"digest": None})])
    notes: list[str] = []
    assert (
        pins.find_update(pin("bsc"), CI_TEXT, upstream.fetch, upstream.digest, notes.append) is None
    )
    assert notes == []


def test_a_digest_that_is_no_sha256_is_an_error():
    upstream = newer_upstream(sha256="not a digest")
    with pytest.raises(pins.PinError, match="SHA-256"):
        pins.find_update(pin("bsc"), CI_TEXT, upstream.fetch, upstream.digest)


def test_a_newer_installer_commit_is_an_update_with_a_compare_link():
    upstream = newer_upstream()
    update = pins.find_update(pin("installer"), OPENXC7_TEXT, upstream.fetch, upstream.digest)
    assert (update.old, update.new) == (OLD_COMMIT, NEW_COMMIT)
    assert update.links == (
        (
            "compare",
            f"https://github.com/openXC7/toolchain-installer/compare/{OLD_COMMIT}...{NEW_COMMIT}",
        ),
    )
    assert pins.apply_update(OPENXC7_TEXT, update) == OPENXC7_TEXT.replace(OLD_COMMIT, NEW_COMMIT)


def test_a_newer_uv_is_an_update_with_its_links():
    upstream = newer_upstream()
    update = pins.find_update(pin("uv"), ORFS_TEXT, upstream.fetch, upstream.digest)
    assert (update.old, update.new) == (OLD_UV, NEW_UV)
    assert dict(update.links) == {
        "release": f"https://github.com/astral-sh/uv/releases/tag/{NEW_UV}",
        "compare": f"https://github.com/astral-sh/uv/compare/{OLD_UV}...{NEW_UV}",
    }
    assert pins.apply_update(ORFS_TEXT, update) == pins.write_uv(ORFS_TEXT, NEW_UV)
    assert pins.read_orfs_image(pins.apply_update(ORFS_TEXT, update))[0] == OLD_ORFS_TAG


def test_uv_never_moves_back_and_a_pin_at_the_newest_is_not_an_update():
    for releases in ([uv_release("0.12.22"), uv_release("0.9.30")], [uv_release(OLD_UV)]):
        upstream = newer_upstream(uv_releases=releases)
        assert pins.find_update(pin("uv"), ORFS_TEXT, upstream.fetch, upstream.digest) is None


def test_a_uv_pin_is_compared_with_the_releases_by_number_not_by_text():
    text = ORFS_TEXT.replace(OLD_UV, "0.9.30")  # older than 0.12.2
    upstream = newer_upstream(uv_releases=[uv_release("0.12.2")])
    update = pins.find_update(pin("uv"), text, upstream.fetch, upstream.digest)
    assert update is not None and update.new == "0.12.2"


def test_a_newer_orfs_image_is_an_update_with_its_links():
    upstream = newer_upstream()
    update = pins.find_update(pin("orfs"), ORFS_TEXT, upstream.fetch, upstream.digest)
    assert (update.old, update.new, update.sha256) == (OLD_ORFS_TAG, NEW_ORFS_TAG, NEW_ORFS_DIGEST)
    assert dict(update.links) == {
        "image": f"https://hub.docker.com/r/openroad/orfs/tags?name={NEW_ORFS_TAG}",
        "compare": "https://github.com/The-OpenROAD-Project/OpenROAD-flow-scripts/compare/"
        "6b305a712...08ddc64f8",
    }
    rewritten = pins.apply_update(ORFS_TEXT, update)
    assert pins.read_orfs_image(rewritten) == (NEW_ORFS_TAG, NEW_ORFS_DIGEST)
    assert upstream.downloads == []  # an image is not downloaded to take its digest


def test_an_orfs_image_never_moves_back():
    older = orfs_answer(orfs_tag("26Q4-130-g479d02aae"), orfs_tag("26Q3-705-gecb3cfdeb"))
    upstream = newer_upstream(orfs_tags=older)
    assert pins.find_update(pin("orfs"), ORFS_TEXT, upstream.fetch, upstream.digest) is None
    # the pinned tag again, with another digest (a tag moved): not an update either
    same = orfs_answer(orfs_tag(OLD_ORFS_TAG, "ab" * 32))
    upstream = newer_upstream(orfs_tags=same)
    assert pins.find_update(pin("orfs"), ORFS_TEXT, upstream.fetch, upstream.digest) is None


def test_an_orfs_pin_is_compared_with_the_tags_by_number_not_by_text():
    text = ORFS_TEXT.replace(OLD_ORFS_TAG, "26Q4-99-g0000099")  # older than 26Q4-100
    upstream = newer_upstream(orfs_tags=orfs_answer(orfs_tag("26Q4-100-g0000100")))
    update = pins.find_update(pin("orfs"), text, upstream.fetch, upstream.digest)
    assert update is not None and update.new == "26Q4-100-g0000100"


@pytest.mark.parametrize("status", ["behind", "diverged"])
def test_an_installer_head_that_is_not_ahead_of_the_pin_is_no_update_and_is_warned_about(status):
    """A rewritten `main` would otherwise stall the pin with no sign of it."""
    upstream = newer_upstream(comparison=status)
    warnings: list[str] = []
    update = pins.find_update(
        pin("installer"), OPENXC7_TEXT, upstream.fetch, upstream.digest, warnings.append
    )
    assert update is None
    assert len(warnings) == 1 and status in warnings[0]
    assert OLD_COMMIT[:7] in warnings[0] and NEW_COMMIT[:7] in warnings[0]


@pytest.mark.parametrize("status", ["behind", "diverged"])
def test_a_run_warns_instead_of_saying_that_an_installer_pin_is_up_to_date(tmp_path, status):
    write_checkout(tmp_path)
    upstream = newer_upstream(comparison=status)
    log: list[str] = []
    warnings: list[str] = []
    pins.run(
        tmp_path,
        upstream.fetch,
        upstream.digest,
        dry_run=True,
        log=log.append,
        warn=warnings.append,
    )
    assert len(warnings) == 1 and "openXC7 toolchain installer" in warnings[0]
    assert status in warnings[0]
    assert not any("installer" in line and "up to date" in line for line in log)
    assert any("OSS CAD Suite" in line and "->" in line for line in log)  # the others go on


def test_a_run_prints_a_warning_as_a_warning_when_no_one_collects_them(tmp_path, capsys):
    write_checkout(tmp_path)
    upstream = newer_upstream(comparison="diverged")
    pins.run(tmp_path, upstream.fetch, upstream.digest, dry_run=True, log=print)
    assert "warning: openXC7 toolchain installer" in capsys.readouterr().out


BSC_WITHOUT_DIGEST = "Bluespec Compiler (bsc): 2026.10 has no published digest; pin it by hand"
BSC_WITHOUT_DIGEST_ORFS = BSC_WITHOUT_DIGEST.replace("(bsc)", "(bsc) for the OpenROAD tests")


def test_a_run_leaves_a_bsc_without_a_published_digest_and_goes_on_with_the_others(tmp_path):
    write_checkout(tmp_path)
    upstream = newer_upstream(bsc_releases=[bsc("2026.10", {"digest": None}), bsc("2026.07.1")])
    log: list[str] = []
    warnings: list[str] = []
    updates = pins.run(
        tmp_path,
        upstream.fetch,
        upstream.digest,
        dry_run=False,
        log=log.append,
        warn=warnings.append,
    )
    assert [u.pin.kind for u in updates] == [
        "oss_cad",
        "nvc",
        "xezim",
        "installer",
        "orfs",
        "uv",
    ]
    assert pins.read_bsc((tmp_path / pins.CI).read_text()) == ("2026.07.1", OLD_SHA256)
    assert pins.read_bsc((tmp_path / pins.ORFS).read_text()) == ("2026.07.1", OLD_UBUNTU_22_SHA256)
    assert warnings == [
        f"{BSC_WITHOUT_DIGEST} (the pin stays at 2026.07.1)",
        f"{BSC_WITHOUT_DIGEST_ORFS} (the pin stays at 2026.07.1)",
    ]
    assert not any("Bluespec" in line and "up to date" in line for line in log)
    assert upstream.downloads == []


def test_a_run_prints_that_warning_as_a_warning_when_nobody_collects_it(tmp_path):
    write_checkout(tmp_path)
    upstream = newer_upstream(bsc_releases=[bsc("2026.10", {"digest": None}), bsc("2026.07.1")])
    log: list[str] = []
    pins.run(tmp_path, upstream.fetch, upstream.digest, dry_run=True, log=log.append)
    assert f"warning: {BSC_WITHOUT_DIGEST} (the pin stays at 2026.07.1)" in log


@pytest.mark.parametrize("answer", [{}, [], {"status": "other"}, {"status": None}, "ahead"])
def test_a_comparison_that_is_no_comparison_is_an_error(answer):
    with pytest.raises(pins.PinError, match="comparison"):
        pins.installer_state(answer)


@pytest.mark.parametrize("status", ["ahead", "behind", "identical", "diverged"])
def test_a_comparison_is_its_status(status):
    assert pins.installer_state({"status": status}) == status


# ------------------------------------------------------------ the download of the bsc tarball

BODY = b"the content of a tarball" * 50
SIZE = len(BODY)
BODY_SHA256 = hashlib.sha256(BODY).hexdigest()
URL = "https://example.org/bsc.tar.gz"


class Response:
    """What `urlopen` gives: a body, and headers like the ones of a server."""

    def __init__(self, body: bytes, headers: dict[str, str]):
        self._body = io.BytesIO(body)
        self.headers = headers

    def read(self, amount: int = -1) -> bytes:
        return self._body.read(amount)

    def __enter__(self):
        return self

    def __exit__(self, *exception):
        return None


def serve(monkeypatch, body: bytes, **headers: str) -> None:
    monkeypatch.setattr(
        pins.urllib.request, "urlopen", lambda request, timeout: Response(body, headers)
    )


def test_a_whole_download_is_hashed(monkeypatch):
    serve(monkeypatch, BODY, **{"Content-Length": str(SIZE)})
    assert pins.download_digest(URL, SIZE, BODY_SHA256) == BODY_SHA256


def test_a_download_needs_no_content_length(monkeypatch):
    serve(monkeypatch, BODY)
    assert pins.download_digest(URL, SIZE, BODY_SHA256) == BODY_SHA256


def test_a_body_cut_short_by_a_clean_close_is_an_error(monkeypatch):
    """`read` returns what arrived and then `b""`, as for a complete body: only the announced
    length tells the difference."""
    serve(monkeypatch, BODY[:40], **{"Content-Length": str(SIZE)})
    with pytest.raises(pins.PinError, match="40"):
        pins.download_digest(URL, SIZE, BODY_SHA256)


def test_a_short_body_is_an_error_when_the_server_announces_no_length(monkeypatch):
    serve(monkeypatch, BODY[:40])
    with pytest.raises(pins.PinError, match="40"):
        pins.download_digest(URL, SIZE, BODY_SHA256)


def test_a_body_of_another_size_than_the_release_lists_is_an_error(monkeypatch):
    serve(monkeypatch, BODY, **{"Content-Length": str(SIZE)})
    with pytest.raises(pins.PinError, match=str(SIZE + 1)):
        pins.download_digest(URL, SIZE + 1, BODY_SHA256)


def test_a_body_of_another_length_than_the_server_announced_is_an_error(monkeypatch):
    serve(monkeypatch, BODY, **{"Content-Length": str(SIZE + 5)})
    with pytest.raises(pins.PinError, match=str(SIZE + 5)):
        pins.download_digest(URL, SIZE, BODY_SHA256)


@pytest.mark.parametrize("length", ["", "abc", "-5", "1e3", "\u0661\u0662"])
def test_a_content_length_that_is_no_number_is_an_error(monkeypatch, length):
    serve(monkeypatch, BODY, **{"Content-Length": length})
    with pytest.raises(pins.PinError, match="Content-Length"):
        pins.download_digest(URL, SIZE, BODY_SHA256)


def test_content_with_another_digest_than_github_lists_is_an_error(monkeypatch):
    serve(monkeypatch, BODY, **{"Content-Length": str(SIZE)})
    with pytest.raises(pins.PinError, match="digest"):
        pins.download_digest(URL, SIZE, "ab" * 32)


def test_an_empty_download_is_an_error(monkeypatch):
    serve(monkeypatch, b"", **{"Content-Length": "0"})
    with pytest.raises(pins.PinError, match="empty"):
        pins.download_digest(URL, 0, BODY_SHA256)


# ------------------------------------------------------------ the lookups of GitHub and Docker Hub


def request_made_for(monkeypatch, path: str, token: str | None):
    requests = []

    def urlopen(request, timeout):
        requests.append(request)
        return Response(b"{}", {})

    monkeypatch.setattr(pins.urllib.request, "urlopen", urlopen)
    assert pins.upstream_fetcher(token)(path) == {}
    (request,) = requests
    return request


def test_a_github_path_goes_to_the_api_with_the_token(monkeypatch):
    request = request_made_for(monkeypatch, "/repos/a/b/releases", "secret")
    assert request.full_url == "https://api.github.com/repos/a/b/releases"
    assert request.get_header("Authorization") == "Bearer secret"


def test_the_tags_of_docker_hub_are_asked_for_without_the_token_of_github(monkeypatch):
    """The token is for GitHub. Docker Hub gets the address and no credentials."""
    request = request_made_for(monkeypatch, pins.ORFS_TAGS_URL, "secret")
    assert request.full_url == pins.ORFS_TAGS_URL
    assert not any("secret" in value for _, value in request.header_items())
    assert request.get_header("Authorization") is None


def test_the_tag_list_asks_docker_hub_for_the_newest_tags_of_the_orfs_image():
    assert pins.ORFS_TAGS_URL == (
        "https://hub.docker.com/v2/repositories/openroad/orfs/tags"
        "?page_size=100&ordering=last_updated"
    )


@pytest.mark.parametrize(
    "path", ["https://example.org/tags", "http://hub.docker.com/v2/x", "repos/a/b", ""]
)
def test_an_address_that_is_neither_github_nor_docker_hub_is_refused(monkeypatch, path):
    monkeypatch.setattr(
        pins.urllib.request, "urlopen", lambda request, timeout: pytest.fail("a request was made")
    )
    with pytest.raises(ValueError, match="address"):
        pins.upstream_fetcher("secret")(path)


# ------------------------------------------------------------------ the files of a checkout


def write_checkout(
    root: Path,
    ci: str | None = CI_TEXT,
    openxc7: str | None = OPENXC7_TEXT,
    orfs: str | None = ORFS_TEXT,
):
    (root / ".github" / "workflows").mkdir(parents=True)
    for path, text in ((pins.CI, ci), (pins.OPENXC7, openxc7), (pins.ORFS, orfs)):
        if text is not None:
            (root / path).write_text(text, encoding="utf-8")


def contents(root: Path) -> dict[str, str]:
    return {
        str(path.relative_to(root)): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*"))
        if path.is_file()
    }


def test_a_dry_run_finds_every_update_and_writes_nothing(tmp_path):
    write_checkout(tmp_path)
    before = contents(tmp_path)
    upstream = newer_upstream()
    log: list[str] = []
    updates = pins.run(tmp_path, upstream.fetch, upstream.digest, dry_run=True, log=log.append)
    assert [u.pin.kind for u in updates] == [
        "oss_cad",
        "nvc",
        "bsc",
        "xezim",
        "installer",
        "orfs",
        "uv",
        "bsc",
    ]
    assert contents(tmp_path) == before
    assert any("2026-09-15 -> 2026-10-07" in line for line in log)
    assert any(NEW_SHA256 in line for line in log)


def test_a_run_writes_the_pins_and_only_the_pins(tmp_path):
    write_checkout(tmp_path)
    upstream = newer_upstream()
    pins.run(tmp_path, upstream.fetch, upstream.digest, dry_run=False, log=lambda line: None)
    after = contents(tmp_path)
    assert after[str(pins.CI)] == pins.write_bsc(
        pins.write_xezim(
            pins.write_nvc(pins.write_oss_cad(CI_TEXT, "2026-10-07"), NEW_NVC), NEW_XEZIM
        ),
        "2026.10",
        NEW_SHA256,
    )
    assert after[str(pins.OPENXC7)] == OPENXC7_TEXT.replace(OLD_COMMIT, NEW_COMMIT)
    assert after[str(pins.ORFS)] == pins.write_bsc(
        pins.write_uv(pins.write_orfs_image(ORFS_TEXT, NEW_ORFS_TAG, NEW_ORFS_DIGEST), NEW_UV),
        "2026.10",
        NEW_SHA256,
    )
    # a second run finds nothing more to do
    again = pins.run(
        tmp_path,
        newer_upstream().fetch,
        newer_upstream().digest,
        dry_run=False,
        log=lambda line: None,
    )
    assert again == [] and contents(tmp_path) == after


def test_a_file_that_does_not_exist_has_no_pin_to_update(tmp_path):
    write_checkout(tmp_path, openxc7=None, orfs=None)
    log: list[str] = []
    upstream = newer_upstream()
    updates = pins.run(tmp_path, upstream.fetch, upstream.digest, dry_run=True, log=log.append)
    assert [u.pin.kind for u in updates] == ["oss_cad", "nvc", "bsc", "xezim"]
    assert any("openxc7.yml" in line and "does not exist" in line for line in log)
    assert any("openroad.yml" in line and "does not exist" in line for line in log)


@pytest.mark.parametrize(
    "files, message",
    [({"openxc7": "jobs: {}\n"}, "INSTALLER_REV"), ({"orfs": "jobs: {}\n"}, "image")],
)
def test_a_file_without_its_pin_is_an_error(tmp_path, files, message):
    write_checkout(tmp_path, **files)
    upstream = newer_upstream()
    with pytest.raises(pins.PinError, match=message):
        pins.run(tmp_path, upstream.fetch, upstream.digest, dry_run=True, log=lambda line: None)


# ------------------------------------------------------------------ the text for people


def test_the_pull_request_and_the_commit_say_what_changed_and_where_to_read_more():
    upstream = newer_upstream()
    updates = [
        pins.find_update(p, text, upstream.fetch, upstream.digest)
        for p, text in zip(
            pins.PINS,
            (CI_TEXT, CI_TEXT, CI_TEXT, CI_TEXT, OPENXC7_TEXT, ORFS_TEXT, ORFS_TEXT, ORFS_TEXT),
        )
    ]
    title, body = pins.pull_request_text(updates)
    message = pins.commit_message(updates)
    assert title == pins.TITLE and message.startswith(f"{pins.TITLE}\n\n")
    for line in (
        "OSS CAD Suite: 2026-09-15 -> 2026-10-07",
        f"NVC: {OLD_NVC} -> {NEW_NVC}",
        "Bluespec Compiler (bsc): 2026.07.1 -> 2026.10",
        "Bluespec Compiler (bsc) for the OpenROAD tests: 2026.07.1 -> 2026.10",
        "openXC7 toolchain installer: efe9e07 -> 1234567",
        f"ORFS image for the OpenROAD tests: {OLD_ORFS_TAG} -> {NEW_ORFS_TAG}",
        f"uv: {OLD_UV} -> {NEW_UV}",
    ):
        assert line in message
    assert "`2026-09-15` | `2026-10-07`" in body
    assert "`efe9e07` | `1234567`" in body
    for url in (
        "https://github.com/YosysHQ/oss-cad-suite-build/releases/tag/2026-10-07",
        f"https://github.com/nickg/nvc/releases/tag/r{NEW_NVC}",
        "https://github.com/B-Lang-org/bsc/releases/tag/2026.10",
        f"https://github.com/openXC7/toolchain-installer/compare/{OLD_COMMIT}...{NEW_COMMIT}",
        f"https://hub.docker.com/r/openroad/orfs/tags?name={NEW_ORFS_TAG}",
        f"https://github.com/astral-sh/uv/releases/tag/{NEW_UV}",
    ):
        assert url in body
    assert f"`{OLD_UV}` | `{NEW_UV}`" in body
    assert f"`{OLD_ORFS_TAG}` | `{NEW_ORFS_TAG}`" in body
    assert f"`sha256:{NEW_ORFS_DIGEST}`" in body and "providers.orfs.version" in body
    assert f"`{NEW_SHA256}`" in body and "bsc-2026.10-ubuntu-24.04.tar.gz" in body
    assert "bsc-2026.10-ubuntu-22.04.tar.gz" in body
    assert "GitHub lists the same digest" in body
    for text in (title, body, message):
        assert "Co-Authored-By" not in text and "Generated with" not in text
        assert all(line == line.rstrip() for line in text.splitlines())


def test_the_text_lists_the_pins_that_stayed_and_why():
    upstream = newer_upstream(bsc_releases=[bsc("2026.10", {"digest": None}), bsc("2026.07.1")])
    update = pins.find_update(pin("oss_cad"), CI_TEXT, upstream.fetch, upstream.digest)
    note = "Bluespec Compiler (bsc): 2026.10 has no published digest; pin it by hand"
    _, body = pins.pull_request_text([update], [note])
    assert f"\n- {note}\n" in body
    _, without = pins.pull_request_text([update])
    assert "These pins stay" not in without and "pin it by hand" not in without
    assert "These pins stay where they are:" in body


def test_the_text_names_only_the_tools_that_changed():
    upstream = newer_upstream(
        bsc_releases=[bsc("2026.07.1")],
        installer_sha=OLD_COMMIT,
        orfs_tags=orfs_answer(orfs_tag(OLD_ORFS_TAG, OLD_ORFS_DIGEST)),
        uv_releases=[uv_release(OLD_UV)],
        nvc_releases=[nvc_release(OLD_NVC)],
        xezim_releases=[xezim_release(OLD_XEZIM)],
    )
    update = pins.find_update(pin("oss_cad"), CI_TEXT, upstream.fetch, upstream.digest)
    _, body = pins.pull_request_text([update])
    assert "OSS CAD Suite" in body
    assert "bsc" not in body and "installer" not in body and "SHA-256" not in body
    assert "orfs" not in body.lower() and "sha256:" not in body and "uv" not in body


# ------------------------------------------------------------------------ the warnings


def test_a_warning_is_a_workflow_command_in_github_actions(monkeypatch, capsys):
    """`::warning::` puts it on the page of the run, not only in the log."""
    monkeypatch.setenv("GITHUB_ACTIONS", "true")
    pins.warn_in_the_log("main (1234567) is behind the pinned commit (efe9e07)")
    assert capsys.readouterr().out == (
        "::warning title=Tool pins::main (1234567) is behind the pinned commit (efe9e07)\n"
    )


@pytest.mark.parametrize("value", [None, "", "false", "1", "True"])
def test_a_warning_is_a_plain_line_anywhere_else(monkeypatch, capsys, value):
    """Only the exact value that GitHub Actions sets makes a workflow command: a person who
    reads the log of a local run sees no `::`."""
    if value is None:
        monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    else:
        monkeypatch.setenv("GITHUB_ACTIONS", value)
    pins.warn_in_the_log("the pin stays")
    assert capsys.readouterr().out == "warning: the pin stays\n"


# ------------------------------------------------------------------------ the command


@pytest.fixture
def command(tmp_path, monkeypatch):
    """`main` on a checkout of old pins, with GitHub and the download replaced."""
    write_checkout(tmp_path)
    upstream = newer_upstream()
    monkeypatch.setattr(pins, "upstream_fetcher", lambda token: upstream.fetch)
    monkeypatch.setattr(pins, "download_digest", upstream.digest)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    return tmp_path


def test_the_command_in_dry_run_prints_the_pull_request_and_writes_nothing(command, capsys):
    before = contents(command)
    assert pins.main(["--root", str(command), "--dry-run"]) == 0
    printed = capsys.readouterr().out
    assert "The pull request would be titled" in printed and "| OSS CAD Suite |" in printed
    assert contents(command) == before


def test_the_command_writes_the_files_and_the_texts(command, tmp_path_factory, monkeypatch):
    out = tmp_path_factory.mktemp("texts")
    output = out / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    body, message = out / "body.md", out / "message.txt"
    argv = ["--root", str(command), "--body-file", str(body), "--message-file", str(message)]
    assert pins.main(argv) == 0
    assert pins.read_oss_cad((command / pins.CI).read_text()) == "2026-10-07"
    assert body.read_text().startswith("This pull request updates tool versions that CI pins.")
    assert message.read_text().startswith(pins.TITLE)
    assert output.read_text() == "changed=true\n"


def test_the_command_tells_the_pull_request_which_pin_stayed(tmp_path, monkeypatch, capsys):
    """The run's log says it, and so does the pull request that the other pins make."""
    write_checkout(tmp_path)
    upstream = newer_upstream(bsc_releases=[bsc("2026.10", {"digest": None}), bsc("2026.07.1")])
    monkeypatch.setattr(pins, "upstream_fetcher", lambda token: upstream.fetch)
    monkeypatch.setattr(pins, "download_digest", upstream.digest)
    monkeypatch.delenv("GITHUB_OUTPUT", raising=False)
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    body, message = tmp_path / "body.md", tmp_path / "message.txt"
    argv = ["--root", str(tmp_path), "--body-file", str(body), "--message-file", str(message)]
    assert pins.main(argv) == 0
    assert f"warning: {BSC_WITHOUT_DIGEST}" in capsys.readouterr().out
    assert f"\n- {BSC_WITHOUT_DIGEST} (the pin stays at 2026.07.1)\n" in body.read_text()
    assert "bsc" not in message.read_text().lower()  # the commit changes the other pins only


def test_the_command_reports_no_change_to_the_workflow(tmp_path, monkeypatch, capsys):
    write_checkout(tmp_path)
    upstream = current_upstream()
    monkeypatch.setattr(pins, "upstream_fetcher", lambda token: upstream.fetch)
    monkeypatch.setattr(pins, "download_digest", upstream.digest)
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert pins.main(["--root", str(tmp_path), "--dry-run"]) == 0
    assert "Nothing to update." in capsys.readouterr().out
    assert output.read_text() == "changed=false\n"


def test_the_command_fails_with_a_message_when_a_pin_cannot_be_read(tmp_path, monkeypatch, capsys):
    write_checkout(tmp_path, ci=CI_TEXT.replace('"2026.07.1"', "2026.07.1"))
    upstream = newer_upstream()
    monkeypatch.setattr(pins, "upstream_fetcher", lambda token: upstream.fetch)
    assert pins.main(["--root", str(tmp_path), "--dry-run"]) == 1
    assert "BSC_VERSION" in capsys.readouterr().err


def test_the_command_fails_with_a_message_when_github_cannot_be_reached(
    tmp_path, monkeypatch, capsys
):
    write_checkout(tmp_path)

    def unreachable(path):
        raise OSError("no network")

    monkeypatch.setattr(pins, "upstream_fetcher", lambda token: unreachable)
    assert pins.main(["--root", str(tmp_path), "--dry-run"]) == 1
    assert "no network" in capsys.readouterr().err


# --------------------------------------------------------------------------- the workflow

WORKFLOW_FILE = ROOT / ".github" / "workflows" / pins.WORKFLOW


def workflow() -> dict[str, Any]:
    return load_yaml(WORKFLOW_FILE)


def steps() -> list[dict[str, Any]]:
    return workflow()["jobs"]["bump"]["steps"]


TOKEN = "CI_PINS_TOKEN"
#: The secret itself in an expression, and the question whether it is set, which hands on nothing.
SECRET_ITSELF = re.compile(r"\$\{\{\s*secrets\." + TOKEN + r"\s*\}\}")
SECRET_ASKED = re.compile(r"\$\{\{\s*secrets\." + TOKEN + r"\s*!=\s*''\s*\}\}")
#: The two commands of the pushing step that get the token, apart from the one that takes it from
#: the environment: the encoder of the credential, and `gh`, each for itself.
TOKEN_USES = re.compile(
    r"""printf 'x-access-token:%s' "\$token" \| base64|\bGH_TOKEN="\$token" gh\b"""
)


def commands(run: str) -> list[str]:
    """The commands of a script, one to a line: a command that spans lines is joined, and
    comments and blank lines are gone."""
    lines = run.replace("\\\n", " ").splitlines()
    return [line.strip() for line in lines if line.strip() and not line.strip().startswith("#")]


def script_problems(run: str, variable: str) -> list[str]:
    """Where the pushing step's script puts the token, which its env gives it as `variable`.

    Every command of a step inherits the env of the step, so the script takes the token into a
    variable of its shell and removes the one of the env before it runs any other command. After
    that, the token goes to the encoder of the credential and to `gh`, each for itself."""
    problems = []
    lines = commands(run)
    taken = (
        len(lines) > 2
        and lines[0].startswith("set -")
        and lines[1] == f'token="${variable}"'
        and lines[2] == f"unset {variable}"
    )
    if not taken:
        problems.append(
            f'the step that pushes does not take the token out of its environment (token="${variable}", '
            f"then unset {variable}) before any other command"
        )
    for line in lines[3:]:
        if re.search(rf"\b{variable}\b", line):
            problems.append(f"the step that pushes reads {variable} after it removed it: {line}")
        if re.search(r"\$\{?token\b", TOKEN_USES.sub("", line)):
            problems.append(f"a command that is not the encoder or gh gets the token: {line}")
    return problems


def token_problems(document: dict[str, Any]) -> list[str]:
    """Everything wrong with where the workflow puts the token, in words (none: all is right).

    The token can push to this repository, so only the commands that need it get it. The step that
    decides whether to change anything asks whether the secret is set, which gives it nothing to
    hand on. Only the step that pushes takes the secret, and only from the env of that step, which
    every command of it inherits, so its script moves the token into a variable of its shell and
    removes the one of the env before it runs any other command. The workflow and the job have no
    token in their env, the step that runs the script on what upstream answers never sees it, and
    the checkout keeps no credential. git gets the credential through its environment, never on
    its command line, where `ps` shows it.
    """
    problems = []
    job = document["jobs"]["bump"]
    for where, env in (("the workflow", document.get("env")), ("the job", job.get("env"))):
        if TOKEN in json.dumps(env):
            problems.append(f"{where} gives the token to every step through its env")
    all_steps = job["steps"]
    pushing = [index for index, step in enumerate(all_steps) if "git push" in step.get("run", "")]
    if len(pushing) != 1:
        problems.append(f"{len(pushing)} steps run git push, and exactly one does")
    for index, step in enumerate(all_steps):
        text = json.dumps(step)
        itself, asked = len(SECRET_ITSELF.findall(text)), len(SECRET_ASKED.findall(text))
        if text.count("secrets." + TOKEN) != itself + asked:
            problems.append(f"step {index} puts the secret into an expression that hands it on")
        if itself and index not in pushing:
            problems.append(f"step {index} gets the secret, which only the step that pushes may")
            if "bump_ci_pins.py" in step.get("run", ""):
                problems.append("the step that runs the script sees the token")
        if index in pushing:
            variables = [
                name
                for name, value in (step.get("env") or {}).items()
                if SECRET_ITSELF.search(str(value))
            ]
            if len(variables) != 1 or itself != 1:
                problems.append(
                    "the step that pushes does not take the token from one env variable"
                )
            else:
                problems += script_problems(step["run"], variables[0])
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            if step.get("with", {}).get("persist-credentials") is not False:
                problems.append("the checkout keeps its credential in .git/config")
        run = step.get("run", "").replace("\\\n", " ")  # a command that spans lines
        if re.search(r"\bgit\b[^\n]*\s-c\s[^\n]*(extraheader|AUTHORIZATION|token)", run, re.I):
            problems.append("a git command line carries the credential (git -c ...)")
        if re.search(r"\bgit\b[^\n]*\bhttps?://[^\s/]*@", run):
            problems.append("a git command line carries the credential in a URL")
        if index in pushing:
            if "GIT_CONFIG_KEY_0" not in run or "extraheader" not in run:
                problems.append("the push does not give git the credential through its environment")
            if "::add-mask::" not in run:
                problems.append("the encoded credential is not masked in the log")
            else:
                first_use = re.search(r"\$\{?basic\b", run)
                if first_use and first_use.start() < run.index("::add-mask::"):
                    problems.append("the encoded credential is used before it is masked")
            if "set -x" in run or "xtrace" in run:
                problems.append("the push step would echo the credential")
    return problems


def test_the_workflow_hands_the_token_only_to_the_commands_that_need_it():
    assert token_problems(workflow()) == []


def _step_with(document, text):
    return next(s for s in document["jobs"]["bump"]["steps"] if text in s.get("run", ""))


def _push_rewritten(old, new):
    """A change of the real workflow: `old` in the script of the pushing step becomes `new`."""

    def mutate(document):
        step = _step_with(document, "git push")
        assert old in step["run"], old
        step["run"] = step["run"].replace(old, new, 1)

    return mutate


def _with_secret_in_env(name, step_text):
    def mutate(document):
        _step_with(document, step_text).setdefault("env", {})[name] = "${{ secrets.CI_PINS_TOKEN }}"

    return mutate


def _checkout(document):
    return next(s for s in document["jobs"]["bump"]["steps"] if "uses" in s)


MUTATIONS = {
    "the token in the env of the workflow": (
        lambda d: d.update(env={"GH_TOKEN": "${{ secrets.CI_PINS_TOKEN }}"}),
        "the workflow gives the token",
    ),
    "the token in the env of the job": (
        lambda d: d["jobs"]["bump"].setdefault("env", {}).update(T="${{ secrets.CI_PINS_TOKEN }}"),
        "the job gives the token",
    ),
    "the token for the step that runs the script": (
        _with_secret_in_env("T", "bump_ci_pins.py"),
        "the step that runs the script sees the token",
    ),
    "the token for the step that decides, as it was": (
        _with_secret_in_env("TOKEN", "gh api"),
        "which only the step that pushes may",
    ),
    "the token for the checkout, or the token of the workflow": (
        lambda d: _checkout(d)["with"].update(token="${{ secrets.CI_PINS_TOKEN || github.token }}"),
        "hands it on",
    ),
    "the checkout keeping its credential": (
        lambda d: _checkout(d)["with"].pop("persist-credentials"),
        "the checkout keeps its credential",
    ),
    "no token for the step that pushes": (
        lambda d: _step_with(d, "git push")["env"].pop("PINS_TOKEN"),
        "does not take the token from one env variable",
    ),
    "the token in the text of the script": (
        _push_rewritten(
            "git fetch", 'curl -H "Authorization: ${{ secrets.CI_PINS_TOKEN }}" x\n  git fetch'
        ),
        "does not take the token from one env variable",
    ),
    "the secret left in the env of every command of the step": (
        lambda d: (
            _push_rewritten('token="$PINS_TOKEN"\nunset PINS_TOKEN\n', "")(d),
            _step_with(d, "git push")["env"].update(GH_TOKEN="${{ secrets.CI_PINS_TOKEN }}"),
            _step_with(d, "git push")["env"].pop("PINS_TOKEN"),
        ),
        "does not take the token out of its environment",
    ),
    "no unset": (
        _push_rewritten("unset PINS_TOKEN\n", ""),
        "does not take the token out of its environment",
    ),
    "the unset after another command": (
        _push_rewritten(
            'unset PINS_TOKEN\ngit config user.name "github-actions[bot]"',
            'git config user.name "github-actions[bot]"\nunset PINS_TOKEN',
        ),
        "does not take the token out of its environment",
    ),
    "the token exported again": (
        _push_rewritten("git switch", 'export GH_TOKEN="$token"\n  git switch'),
        "gets the token",
    ),
    "the token echoed": (
        _push_rewritten("git switch", 'echo "$token"\n  git switch'),
        "gets the token",
    ),
    "the token given to a git command": (
        _push_rewritten("git switch", 'GH_TOKEN="$token" git switch'),
        "gets the token",
    ),
    "the variable of the env read after the unset": (
        _push_rewritten('GH_TOKEN="$token" gh pr list', 'GH_TOKEN="$PINS_TOKEN" gh pr list'),
        "after it removed it",
    ),
    "the credential on a git command line": (
        _push_rewritten(
            "git push",
            'git -c "http.https://github.com/.extraheader=AUTHORIZATION: basic $basic" push',
        ),
        "carries the credential",
    ),
    "the credential on a git command line that spans lines": (
        _push_rewritten(
            "git push", 'git \\\n -c "http.https://github.com/.extraheader=$basic" push'
        ),
        "carries the credential",
    ),
    "the credential in the URL of the push": (
        _push_rewritten(
            "git push --force origin", "git push --force https://x:$token@github.com/o/r.git"
        ),
        "in a URL",
    ),
    "the encoded credential shown before it is masked": (
        _push_rewritten('echo "::add-mask::$basic"', 'echo "$basic"\n  echo "::add-mask::$basic"'),
        "used before it is masked",
    ),
    "the log showing the credential": (
        _push_rewritten("set -euo pipefail", "set -x\n  set -euo pipefail"),
        "would echo the credential",
    ),
    "the encoded credential not masked": (
        _push_rewritten("::add-mask::", "::notice::"),
        "not masked",
    ),
}


@pytest.mark.parametrize("name", MUTATIONS)
def test_the_token_oracle_sees_each_way_to_hand_the_token_out(name):
    """The oracle has teeth: it reports every change here to the real workflow."""
    mutate, expected = MUTATIONS[name]
    document = workflow()
    mutate(document)
    assert any(expected in problem for problem in token_problems(document)), token_problems(
        document
    )


@pytest.mark.skipif(shutil.which("jq") is None, reason="the lookup is a jq expression")
@pytest.mark.parametrize(
    "listed, expected",
    [
        pytest.param([], "", id="none"),
        pytest.param([{"number": 7, "isCrossRepository": False}], "7", id="one of ours"),
        pytest.param([{"number": 7, "isCrossRepository": True}], "", id="one from a fork"),
        pytest.param(
            [
                {"number": 7, "isCrossRepository": True},
                {"number": 9, "isCrossRepository": False},
            ],
            "9",
            id="a fork's first",
        ),
    ],
)
def test_the_pull_request_lookup_ignores_the_pull_requests_of_forks(listed, expected):
    """`gh pr list --head` matches the name of the branch only, so a fork's pull request from a
    branch of the same name would be taken for ours and edited."""
    lookup = next(s["run"] for s in steps() if "gh pr list" in s.get("run", ""))
    assert "--json number,isCrossRepository" in lookup
    expression = re.search(r"--jq '([^']+)'", lookup)
    assert expression is not None
    answer = subprocess.run(
        ["jq", "-r", expression.group(1)],
        input=json.dumps(listed),
        capture_output=True,
        text=True,
        check=True,
    )
    assert answer.stdout.strip() == expected


# ------------------------------------------------------------- the workflow of the OpenROAD job

OPENROAD_WORKFLOW = ROOT / pins.ORFS


def openroad_workflow() -> dict[str, Any]:
    return load_yaml(OPENROAD_WORKFLOW)


def openroad_job() -> dict[str, Any]:
    jobs = openroad_workflow()["jobs"]
    assert list(jobs) == ["tests"]
    return jobs["tests"]


def openroad_step(text: str) -> dict[str, Any]:
    """The only step whose name or command holds `text`."""
    found = [
        step
        for step in openroad_job()["steps"]
        if text in step.get("name", "") or text in step.get("run", "")
    ]
    assert len(found) == 1, f"{len(found)} steps hold {text!r}"
    return found[0]


def test_the_openroad_job_runs_in_the_image_that_the_script_pins():
    """The `image` line is the pin the script reads and writes, and it names the image by its
    digest: a tag on Docker Hub can move."""
    tag, digest = pins.read_orfs_image(OPENROAD_WORKFLOW.read_text(encoding="utf-8"))
    assert openroad_job()["container"]["image"] == f"openroad/orfs@sha256:{digest}"
    assert pins.ORFS_TAG.fullmatch(tag)


def test_the_openroad_job_is_one_pinned_job_that_only_reads():
    document = openroad_workflow()
    job = openroad_job()
    assert document["permissions"] == {"contents": "read"} and "permissions" not in job
    assert job["runs-on"] == "ubuntu-24.04"  # not ubuntu-latest
    assert job["timeout-minutes"] == 45
    triggers = document["on"]
    assert set(triggers) == {"push", "pull_request", "workflow_dispatch"}
    for event in ("push", "pull_request"):
        assert triggers[event] == {"branches": ["main", "release/*"]}


def test_every_action_of_the_openroad_job_names_a_version():
    uses = [step["uses"] for step in openroad_job()["steps"] if "uses" in step]
    assert uses, "the job uses no action"
    for used in uses:
        assert re.fullmatch(r"[\w.-]+/[\w.-]+@v[0-9][\w.]*", used), used
    checkout = next(step for step in openroad_job()["steps"] if "checkout" in step.get("uses", ""))
    assert checkout["with"]["fetch-depth"] == 0  # xeda's version comes from the git tags


def test_the_openroad_job_has_no_pin_of_the_platform_files_of_its_own():
    """The PDK files come from `xeda pdk install`, one call for each platform of
    `XEDA_TESTS_PLATFORMS`, at the version that the platform file of the checkout pins, so the job
    tests the installer and cannot disagree with the pin."""
    text = OPENROAD_WORKFLOW.read_text(encoding="utf-8")
    install = openroad_step("Install the PDK files")["run"]
    assert "for platform in ${XEDA_TESTS_PLATFORMS//,/ }; do" in install
    assert re.search(r'xeda pdk install "\$platform" --root "\$RUNNER_TEMP/pdk"', install)
    assert "--version" not in install and "--provider" not in install
    assert "--platform-file" not in install
    # the second provider of sky130hd has a step of its own, at the pin of the platform file
    second = openroad_step("Install sky130hd from lambdapdk")["run"]
    assert (
        'xeda pdk install sky130hd --provider lambdapdk --root "$RUNNER_TEMP/pdk" --yes' in second
    )
    assert "--version" not in second and "--platform-file" not in second
    assert "ORFS_PLATFORM_REV" not in text
    assert not re.search(r"\bgit (clone|fetch|init|sparse-checkout)\b", text)
    # the only digests and commits in the file are the image's and the bsc tarball's
    assert not re.search(r"\b[0-9a-f]{40}\b", text)  # no commit
    assert len(re.findall(r"\b[0-9a-f]{64}\b", text)) == 2  # the image and the bsc tarball


def test_the_openroad_job_runs_the_real_tool_tests_with_every_layer_switched_on():
    job = openroad_job()
    environment = job["env"]
    assert environment["XEDA_TESTS_OPENROAD"] == "1"
    assert environment["XEDA_TESTS_REQUIRE_TOOLS"] == "1"
    # every bundled platform, so a platform that xeda ships is tested with its real PDK files
    assert environment["XEDA_TESTS_PLATFORMS"].split(",") == bundled_platform_names()
    # the platforms that a second provider serves, with that provider's own files
    assert environment["XEDA_TESTS_PDK_PROVIDERS"] == "sky130hd:lambdapdk"
    run = openroad_step("Run the OpenROAD tests")["run"]
    assert 'XEDA_TESTS_PDK_ROOT="$RUNNER_TEMP/pdk"' in run
    # the store the install step fills is the store the tests read
    assert '--root "$RUNNER_TEMP/pdk"' in openroad_step("Install the PDK files")["run"]
    for test_file in (
        "tests/test_openroad_real.py",
        "tests/test_platform_conformance.py",
        "tests/test_yosys_asic.py",
        "tests/test_yosys_asic_tcl.py",
    ):
        assert test_file in run
    assert "-rs" in run and "--junitxml" in run


def test_the_openroad_job_runs_the_tcl_tests_of_yosys_only_when_the_probe_passed():
    probe = openroad_step("Check whether yosys runs a Tcl script")["run"]
    assert 'yosys -q -c "$RUNNER_TEMP/probe.tcl"' in probe  # as tests/tool_utils.py asks it
    assert probe.count('echo "XEDA_TESTS_YOSYS_TCL=1" >> "$GITHUB_ENV"') == 1
    assert "XEDA_TESTS_YOSYS_TCL" not in openroad_job()["env"]
    run = openroad_step("Run the OpenROAD tests")["run"]
    guarded = re.search(
        r'if \[ "\$\{XEDA_TESTS_YOSYS_TCL:-\}" = 1 \]; then\n\s+files\+=\(tests/test_yosys_asic_tcl.py\)',
        run,
    )
    assert guarded and run.count("test_yosys_asic_tcl.py") == 1


def test_the_openroad_job_puts_the_tools_of_the_image_first_and_its_own_python_last():
    """The yosys that OpenROAD-flow-scripts builds, not the one in /usr/local/bin, and the
    tools run `python3` of the image, not the venv's."""
    path = openroad_step("Put the tools of the image on PATH")["run"]
    assert '"$ORFS_INSTALL/OpenROAD/bin" >> "$GITHUB_PATH"' in path
    assert '"$ORFS_INSTALL/yosys/bin" >> "$GITHUB_PATH"' in path
    assert openroad_job()["env"]["ORFS_INSTALL"] == "/OpenROAD-flow-scripts/tools/install"
    for step in openroad_job()["steps"]:
        for line in step.get("run", "").splitlines():
            if "venv/bin" in line and "python" not in line:
                assert line.strip() == 'export PATH="$PATH:$RUNNER_TEMP/venv/bin"', line


SKIP_CHECK = re.search(
    r"<<'PY'\n(?P<code>.*?)\nPY\n?", openroad_step("Run the OpenROAD tests")["run"], re.DOTALL
)


def junit(*cases: tuple[str, str | None]) -> str:
    """A JUnit report: each case is a name and the reason for which it skipped, or None."""
    body = "".join(
        f'<testcase classname="tests.test_x" name="{name}">'
        + (f'<skipped type="pytest.skip" message="{reason}"/>' if reason is not None else "")
        + "</testcase>"
        for name, reason in cases
    )
    return (
        f'<?xml version="1.0"?><testsuites><testsuite name="pytest">{body}</testsuite></testsuites>'
    )


def skip_check(tmp_path: Path, report: str, allowed: str | None = None):
    assert SKIP_CHECK, "the test step has no check of the skipped tests"
    path = tmp_path / "report.xml"
    path.write_text(report, encoding="utf-8")
    environment = {"PATH": "/usr/bin:/bin"}
    if allowed is not None:
        environment["ALLOWED_SKIPS"] = allowed
    return subprocess.run(
        [sys.executable, "-I", "-", str(path)],
        input=SKIP_CHECK["code"],
        capture_output=True,
        text=True,
        env=environment,
    )


def test_the_job_passes_when_no_test_skipped(tmp_path):
    result = skip_check(tmp_path, junit(("test_a", None), ("test_b", None)))
    assert result.returncode == 0, result.stderr


def test_the_job_fails_when_a_test_skipped(tmp_path):
    result = skip_check(tmp_path, junit(("test_a", None), ("test_b", "set XEDA_TESTS_OPENROAD=1")))
    assert result.returncode != 0
    assert "test_b" in result.stderr and "XEDA_TESTS_OPENROAD" in result.stderr


def test_the_job_fails_when_no_test_ran(tmp_path):
    assert skip_check(tmp_path, junit()).returncode != 0


def test_a_skip_that_the_probe_allowed_does_not_fail_the_job_and_another_does(tmp_path):
    allowed = "this yosys has no TCL support"
    tcl = ("test_tcl", "this yosys has no TCL support")
    assert skip_check(tmp_path, junit(("test_a", None), tcl), allowed).returncode == 0
    other = ("test_b", "set XEDA_TESTS_OPENROAD=1")
    result = skip_check(tmp_path, junit(tcl, other), allowed)
    assert result.returncode != 0 and "test_b" in result.stderr and "test_tcl" not in result.stderr
