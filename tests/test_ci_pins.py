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
NEW_SHA256 = "ab" * 32
OLD_COMMIT = "efe9e07330e88434a831f2c52bee72739da0f0a5"
NEW_COMMIT = "1234567890abcdef1234567890abcdef12345678"

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
          version: latest
      - name: Install Bluespec Compiler (bsc)
        env:
          BSC_VERSION: "2026.07.1"
          BSC_SHA256: "{OLD_SHA256}"
        run: |
          curl -fsSL -o bsc.tar.gz "https://example.org/${{BSC_VERSION}}/bsc.tar.gz"
          echo "${{BSC_SHA256}}  bsc.tar.gz" | sha256sum -c -
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


def bsc(tag: str, asset: dict[str, Any] | None = None, **flags: bool) -> dict[str, Any]:
    """A bsc release. `asset` adds to, or replaces, the fields of its Ubuntu 24.04 tarball."""
    tarball = {"name": f"bsc-{tag}-ubuntu-24.04.tar.gz", "size": TARBALL_SIZE, **(asset or {})}
    return release(tag, tarball, **flags)


class Upstream:
    """What GitHub says, and the digest of the tarball, for the functions that ask."""

    def __init__(
        self, oss_cad_releases, bsc_releases, installer_sha, sha256=NEW_SHA256, comparison="ahead"
    ):
        self.answers = {
            f"/repos/{pins.OSS_CAD_REPOSITORY}/releases?per_page=30": oss_cad_releases,
            f"/repos/{pins.BSC_REPOSITORY}/releases?per_page=30": bsc_releases,
            f"/repos/{pins.INSTALLER_REPOSITORY}/commits/main": {"sha": installer_sha},
            f"/repos/{pins.INSTALLER_REPOSITORY}/compare/{OLD_COMMIT}...{installer_sha}": {
                "status": comparison
            },
        }
        self.sha256 = sha256
        self.downloads: list[str] = []
        self.checks: list[tuple[int, str | None]] = []  # the size and digest each download got

    def fetch(self, path: str) -> Any:
        return self.answers[path]

    def digest(self, url: str, size: int, sha256: str | None) -> str:
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
    return Upstream([oss_cad("2026-09-15")], [bsc("2026.07.1")], OLD_COMMIT)


def pin(kind: str):
    return next(p for p in pins.PINS if p.kind == kind)


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
    elif pin_.kind == "bsc":
        version, sha256 = pins.read_bsc(text)
        assert pins.BSC_TAG.fullmatch(version) and pins.SHA256.fullmatch(sha256)
    else:
        assert pin_.kind == "installer"
        assert pins.COMMIT.fullmatch(pins.read_installer(text))


def test_ci_downloads_the_bsc_tarball_from_the_address_the_script_hashes():
    """`bsc_url` builds the address the way ci.yml does from BSC_VERSION, so the SHA-256 that the
    script computes is the one the step checks."""
    assert pins.bsc_url("${BSC_VERSION}") in (ROOT / pins.CI).read_text(encoding="utf-8")


# ------------------------------------------------------------------------ reading a pin


def test_each_pin_is_read():
    assert pins.read_oss_cad(CI_TEXT) == "2026-09-15"
    assert pins.read_bsc(CI_TEXT) == ("2026.07.1", OLD_SHA256)
    assert pins.read_installer(OPENXC7_TEXT) == OLD_COMMIT


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


@pytest.mark.parametrize(
    "text, reader, message",
    [
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
            CI_TEXT.replace('"2026-09-15"', '"2026-09-15"'.translate(ARABIC_INDIC)),
            "oss",
            "version",
            id="other digits",
        ),
    ],
)
def test_a_pin_in_another_format_is_an_error_not_a_guess(text, reader, message):
    read = {"oss": pins.read_oss_cad, "bsc": pins.read_bsc, "installer": pins.read_installer}
    with pytest.raises(pins.PinError, match=message):
        read[reader](text)


# --------------------------------------------------------------------- replacing a pin


def test_replacing_a_pin_changes_that_line_and_no_other():
    assert pins.write_oss_cad(CI_TEXT, "2026-10-07").replace("2026-10-07", "2026-09-15") == CI_TEXT
    rewritten = pins.write_bsc(CI_TEXT, "2026.10", NEW_SHA256)
    assert pins.read_bsc(rewritten) == ("2026.10", NEW_SHA256)
    assert rewritten.replace("2026.10", "2026.07.1").replace(NEW_SHA256, OLD_SHA256) == CI_TEXT
    rewritten = pins.write_installer(OPENXC7_TEXT, NEW_COMMIT)
    assert rewritten.replace(NEW_COMMIT, OLD_COMMIT) == OPENXC7_TEXT


def test_replacing_a_pin_keeps_the_quotes_and_the_line_endings():
    text = OPENXC7_TEXT.replace(OLD_COMMIT, f'"{OLD_COMMIT}"').replace("\n", "\r\n")
    rewritten = pins.write_installer(text, NEW_COMMIT)
    assert rewritten == text.replace(OLD_COMMIT, NEW_COMMIT)
    assert f'INSTALLER_REV: "{NEW_COMMIT}"\r\n' in rewritten


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
    assert pins.newest_bsc([bsc("2026.07.1"), bsc(numbered)]).tag == "2026.07.1"
    assert not pins.OSS_CAD_TAG.fullmatch(dated) and not pins.BSC_TAG.fullmatch(numbered)


@pytest.mark.parametrize("answer", [[], {"message": "rate limit"}, "text", [1, 2], [release("x")]])
def test_no_usable_oss_cad_release_is_an_error(answer):
    with pytest.raises(pins.PinError):
        pins.newest_oss_cad(answer)


def test_the_newest_bsc_release_has_the_ubuntu_24_04_tarball():
    answer = [
        bsc("2026.01"),
        bsc("2026.07"),
        bsc("2026.07.1"),
        release("2026.12", "bsc-2026.12-ubuntu-24.04-arm.tar.gz"),  # another architecture
        release("2026.11", "bsc-2026.11-ubuntu-22.04.tar.gz"),  # another Ubuntu
        bsc("2026.10", prerelease=True),
        bsc("2026.09", draft=True),
        bsc("2026.08-rc1"),  # not a release number
        bsc("nightly"),
    ]
    assert pins.newest_bsc(answer).tag == "2026.07.1"


def test_bsc_releases_are_ordered_as_numbers_not_as_text():
    assert pins.newest_bsc([bsc("2026.07"), bsc("2026.07.1")]).tag == "2026.07.1"
    assert pins.newest_bsc([bsc("2025.12.3"), bsc("2025.12.10")]).tag == "2025.12.10"


@pytest.mark.parametrize("answer", [[], {"message": "rate limit"}, [release("2026.01")]])
def test_no_usable_bsc_release_is_an_error(answer):
    with pytest.raises(pins.PinError):
        pins.newest_bsc(answer)


def test_the_tarball_of_a_release_has_the_size_and_the_digest_github_lists_for_it():
    answer = [bsc("2026.10", {"size": 1234, "digest": f"sha256:{NEW_SHA256}"}), bsc("2026.07.1")]
    tarball = pins.newest_bsc(answer)
    assert (tarball.tag, tarball.size, tarball.sha256) == ("2026.10", 1234, NEW_SHA256)


@pytest.mark.parametrize("asset", [{}, {"digest": None}], ids=["no digest", "digest null"])
def test_a_tarball_that_github_gives_no_digest_has_none(asset):
    tarball = pins.newest_bsc([bsc("2026.10", asset)])
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
        pins.newest_bsc([bsc("2026.10", asset)])


@pytest.mark.parametrize("answer", [{}, [], {"sha": None}, {"sha": "main"}, {"sha": "ab" * 21}])
def test_an_installer_answer_that_is_no_commit_is_an_error(answer):
    with pytest.raises(pins.PinError):
        pins.installer_head(answer)


def test_an_installer_commit_is_taken_whole():
    assert pins.installer_head({"sha": NEW_COMMIT}) == NEW_COMMIT


# ----------------------------------------------------------- finding and applying updates


def test_nothing_is_updated_when_upstream_is_where_the_pins_are():
    upstream = current_upstream()
    for kind, text in (("oss_cad", CI_TEXT), ("bsc", CI_TEXT), ("installer", OPENXC7_TEXT)):
        assert pins.find_update(pin(kind), text, upstream.fetch, upstream.digest) is None
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


def test_a_newer_bsc_is_hashed_from_the_address_ci_downloads():
    upstream = newer_upstream()
    update = pins.find_update(pin("bsc"), CI_TEXT, upstream.fetch, upstream.digest)
    assert (update.old, update.new, update.sha256) == ("2026.07.1", "2026.10", NEW_SHA256)
    assert upstream.downloads == [
        "https://github.com/B-Lang-org/bsc/releases/download/2026.10/bsc-2026.10-ubuntu-24.04.tar.gz"
    ]
    assert pins.read_bsc(pins.apply_update(CI_TEXT, update)) == ("2026.10", NEW_SHA256)


def test_a_newer_bsc_is_downloaded_against_the_size_and_digest_github_lists():
    listed = {"size": 4321, "digest": f"sha256:{NEW_SHA256}"}
    upstream = newer_upstream(bsc_releases=[bsc("2026.10", listed), bsc("2026.07.1")])
    pins.find_update(pin("bsc"), CI_TEXT, upstream.fetch, upstream.digest)
    assert upstream.checks == [(4321, NEW_SHA256)]


def test_a_newer_bsc_without_a_listed_digest_is_downloaded_against_its_size():
    upstream = newer_upstream()
    pins.find_update(pin("bsc"), CI_TEXT, upstream.fetch, upstream.digest)
    assert upstream.checks == [(TARBALL_SIZE, None)]


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


def test_a_download_needs_neither_a_content_length_nor_a_listed_digest(monkeypatch):
    serve(monkeypatch, BODY)
    assert pins.download_digest(URL, SIZE, None) == BODY_SHA256


def test_a_body_cut_short_by_a_clean_close_is_an_error(monkeypatch):
    """`read` returns what arrived and then `b""`, as for a complete body: only the announced
    length tells the difference."""
    serve(monkeypatch, BODY[:40], **{"Content-Length": str(SIZE)})
    with pytest.raises(pins.PinError, match="40"):
        pins.download_digest(URL, SIZE, None)


def test_a_short_body_is_an_error_when_the_server_announces_no_length(monkeypatch):
    serve(monkeypatch, BODY[:40])
    with pytest.raises(pins.PinError, match="40"):
        pins.download_digest(URL, SIZE, None)


def test_a_body_of_another_size_than_the_release_lists_is_an_error(monkeypatch):
    serve(monkeypatch, BODY, **{"Content-Length": str(SIZE)})
    with pytest.raises(pins.PinError, match=str(SIZE + 1)):
        pins.download_digest(URL, SIZE + 1, None)


def test_a_body_of_another_length_than_the_server_announced_is_an_error(monkeypatch):
    serve(monkeypatch, BODY, **{"Content-Length": str(SIZE + 5)})
    with pytest.raises(pins.PinError, match=str(SIZE + 5)):
        pins.download_digest(URL, SIZE, None)


@pytest.mark.parametrize("length", ["", "abc", "-5", "1e3", "\u0661\u0662"])
def test_a_content_length_that_is_no_number_is_an_error(monkeypatch, length):
    serve(monkeypatch, BODY, **{"Content-Length": length})
    with pytest.raises(pins.PinError, match="Content-Length"):
        pins.download_digest(URL, SIZE, None)


def test_content_with_another_digest_than_github_lists_is_an_error(monkeypatch):
    serve(monkeypatch, BODY, **{"Content-Length": str(SIZE)})
    with pytest.raises(pins.PinError, match="digest"):
        pins.download_digest(URL, SIZE, "ab" * 32)


def test_an_empty_download_is_an_error(monkeypatch):
    serve(monkeypatch, b"", **{"Content-Length": "0"})
    with pytest.raises(pins.PinError, match="empty"):
        pins.download_digest(URL, 0, None)


# ------------------------------------------------------------------ the files of a checkout


def write_checkout(root: Path, ci: str | None = CI_TEXT, openxc7: str | None = OPENXC7_TEXT):
    (root / ".github" / "workflows").mkdir(parents=True)
    for path, text in ((pins.CI, ci), (pins.OPENXC7, openxc7)):
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
    assert [u.pin.kind for u in updates] == ["oss_cad", "bsc", "installer"]
    assert contents(tmp_path) == before
    assert any("2026-09-15 -> 2026-10-07" in line for line in log)
    assert any(NEW_SHA256 in line for line in log)


def test_a_run_writes_the_pins_and_only_the_pins(tmp_path):
    write_checkout(tmp_path)
    upstream = newer_upstream()
    pins.run(tmp_path, upstream.fetch, upstream.digest, dry_run=False, log=lambda line: None)
    after = contents(tmp_path)
    assert after[str(pins.CI)] == pins.write_bsc(
        pins.write_oss_cad(CI_TEXT, "2026-10-07"), "2026.10", NEW_SHA256
    )
    assert after[str(pins.OPENXC7)] == OPENXC7_TEXT.replace(OLD_COMMIT, NEW_COMMIT)
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
    write_checkout(tmp_path, openxc7=None)
    log: list[str] = []
    upstream = newer_upstream()
    updates = pins.run(tmp_path, upstream.fetch, upstream.digest, dry_run=True, log=log.append)
    assert [u.pin.kind for u in updates] == ["oss_cad", "bsc"]
    assert any("openxc7.yml" in line and "does not exist" in line for line in log)


def test_a_file_without_its_pin_is_an_error(tmp_path):
    write_checkout(tmp_path, openxc7="jobs: {}\n")
    upstream = newer_upstream()
    with pytest.raises(pins.PinError, match="INSTALLER_REV"):
        pins.run(tmp_path, upstream.fetch, upstream.digest, dry_run=True, log=lambda line: None)


# ------------------------------------------------------------------ the text for people


def test_the_pull_request_and_the_commit_say_what_changed_and_where_to_read_more():
    upstream = newer_upstream()
    updates = [
        pins.find_update(p, text, upstream.fetch, upstream.digest)
        for p, text in zip(pins.PINS, (CI_TEXT, CI_TEXT, OPENXC7_TEXT))
    ]
    title, body = pins.pull_request_text(updates)
    message = pins.commit_message(updates)
    assert title == pins.TITLE and message.startswith(f"{pins.TITLE}\n\n")
    for line in (
        "OSS CAD Suite: 2026-09-15 -> 2026-10-07",
        "Bluespec Compiler (bsc): 2026.07.1 -> 2026.10",
        "openXC7 toolchain installer: efe9e07 -> 1234567",
    ):
        assert line in message
    assert "`2026-09-15` | `2026-10-07`" in body
    assert "`efe9e07` | `1234567`" in body
    for url in (
        "https://github.com/YosysHQ/oss-cad-suite-build/releases/tag/2026-10-07",
        "https://github.com/B-Lang-org/bsc/releases/tag/2026.10",
        f"https://github.com/openXC7/toolchain-installer/compare/{OLD_COMMIT}...{NEW_COMMIT}",
    ):
        assert url in body
    assert f"`{NEW_SHA256}`" in body and "bsc-2026.10-ubuntu-24.04.tar.gz" in body
    assert "GitHub lists no digest" in body  # this release lists none
    for text in (title, body, message):
        assert "Co-Authored-By" not in text and "Generated with" not in text
        assert all(line == line.rstrip() for line in text.splitlines())


def test_the_text_says_when_github_lists_the_same_digest():
    listed = {"digest": f"sha256:{NEW_SHA256}"}
    upstream = newer_upstream(bsc_releases=[bsc("2026.10", listed), bsc("2026.07.1")])
    update = pins.find_update(pin("bsc"), CI_TEXT, upstream.fetch, upstream.digest)
    _, body = pins.pull_request_text([update])
    assert "GitHub lists the same digest" in body and "no digest" not in body


def test_the_text_names_only_the_tools_that_changed():
    upstream = newer_upstream(bsc_releases=[bsc("2026.07.1")], installer_sha=OLD_COMMIT)
    update = pins.find_update(pin("oss_cad"), CI_TEXT, upstream.fetch, upstream.digest)
    _, body = pins.pull_request_text([update])
    assert "OSS CAD Suite" in body
    assert "bsc" not in body and "installer" not in body and "SHA-256" not in body


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
    monkeypatch.setattr(pins, "github_fetcher", lambda token: upstream.fetch)
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


def test_the_command_reports_no_change_to_the_workflow(tmp_path, monkeypatch, capsys):
    write_checkout(tmp_path)
    upstream = current_upstream()
    monkeypatch.setattr(pins, "github_fetcher", lambda token: upstream.fetch)
    monkeypatch.setattr(pins, "download_digest", upstream.digest)
    output = tmp_path / "github-output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(output))
    assert pins.main(["--root", str(tmp_path), "--dry-run"]) == 0
    assert "Nothing to update." in capsys.readouterr().out
    assert output.read_text() == "changed=false\n"


def test_the_command_fails_with_a_message_when_a_pin_cannot_be_read(tmp_path, monkeypatch, capsys):
    write_checkout(tmp_path, ci=CI_TEXT.replace('"2026.07.1"', "2026.07.1"))
    upstream = newer_upstream()
    monkeypatch.setattr(pins, "github_fetcher", lambda token: upstream.fetch)
    assert pins.main(["--root", str(tmp_path), "--dry-run"]) == 1
    assert "BSC_VERSION" in capsys.readouterr().err


def test_the_command_fails_with_a_message_when_github_cannot_be_reached(
    tmp_path, monkeypatch, capsys
):
    write_checkout(tmp_path)

    def unreachable(path):
        raise OSError("no network")

    monkeypatch.setattr(pins, "github_fetcher", lambda token: unreachable)
    assert pins.main(["--root", str(tmp_path), "--dry-run"]) == 1
    assert "no network" in capsys.readouterr().err


# --------------------------------------------------------------------------- the workflow

WORKFLOW_FILE = ROOT / ".github" / "workflows" / pins.WORKFLOW


def workflow() -> dict[str, Any]:
    return load_yaml(WORKFLOW_FILE)


def steps() -> list[dict[str, Any]]:
    return workflow()["jobs"]["bump"]["steps"]


TOKEN = "CI_PINS_TOKEN"


def token_problems(document: dict[str, Any]) -> list[str]:
    """Everything wrong with where the workflow puts the token, in words (none: all is right).

    The token can push to this repository. It reaches two steps and no other: the step that
    decides whether to change anything reads that it is set, and the step that pushes uses it.
    So it is in no `env` of the workflow or of the job, which every step would see, nor in the
    step that runs the script on what upstream answers. The checkout keeps no credential, and a
    git command gets the token through its environment, never on its command line, where `ps`
    shows it.
    """
    problems = []
    job = document["jobs"]["bump"]
    for where, env in (("the workflow", document.get("env")), ("the job", job.get("env"))):
        if TOKEN in json.dumps(env):
            problems.append(f"{where} gives the token to every step through its env")
    all_steps = job["steps"]
    needs = [
        index
        for index, step in enumerate(all_steps)
        if "git push" in step.get("run", "") or '[ -z "$TOKEN" ]' in step.get("run", "")
    ]
    holders = [index for index, step in enumerate(all_steps) if TOKEN in json.dumps(step)]
    if len(needs) != 2 or holders != needs:
        problems.append(
            f"steps {holders} hold the token, and exactly the steps that push and that check "
            f"it is set ({needs}) are to"
        )
    for step in all_steps:
        if "bump_ci_pins.py" in step.get("run", "") and TOKEN in json.dumps(step):
            problems.append("the step that runs the script sees the token")
        if str(step.get("uses", "")).startswith("actions/checkout@"):
            if step.get("with", {}).get("persist-credentials") is not False:
                problems.append("the checkout keeps its credential in .git/config")
        run = step.get("run", "").replace("\\\n", " ")  # a command that spans lines
        if re.search(r"\bgit\b[^\n]*\s-c\s[^\n]*(extraheader|AUTHORIZATION|token)", run, re.I):
            problems.append("a git command line carries the credential (git -c ...)")
        if re.search(r"\bgit\b[^\n]*\bhttps?://[^\s/]*@", run):
            problems.append("a git command line carries the credential in a URL")
        if "git push" in run:
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


def test_the_workflow_hands_the_token_only_to_the_steps_that_need_it():
    assert token_problems(workflow()) == []


def _drop_persist_credentials(document):
    checkout = next(s for s in document["jobs"]["bump"]["steps"] if "uses" in s)
    checkout["with"].pop("persist-credentials")


def _step_with(document, text):
    return next(s for s in document["jobs"]["bump"]["steps"] if text in s.get("run", ""))


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
        lambda d: _step_with(d, "bump_ci_pins.py")
        .setdefault("env", {})
        .update(T="${{ secrets.CI_PINS_TOKEN }}"),
        "the step that runs the script sees the token",
    ),
    "the token for the checkout": (
        lambda d: _drop_persist_credentials(d),
        "the checkout keeps its credential",
    ),
    "no token for the step that pushes": (
        lambda d: _step_with(d, "git push").update(env={}),
        "hold the token",
    ),
    "the credential on a git command line": (
        lambda d: _step_with(d, "git push").update(
            run=_step_with(d, "git push")["run"].replace(
                "git push",
                'git -c "http.https://github.com/.extraheader=AUTHORIZATION: basic $basic" push',
            )
        ),
        "carries the credential",
    ),
    "the credential on a git command line that spans lines": (
        lambda d: _step_with(d, "git push").update(
            run=_step_with(d, "git push")["run"].replace(
                "git push", 'git \\\n -c "http.https://github.com/.extraheader=$basic" push'
            )
        ),
        "carries the credential",
    ),
    "the credential in the URL of the push": (
        lambda d: _step_with(d, "git push").update(
            run=_step_with(d, "git push")["run"].replace(
                "git push --force origin", "git push --force https://x:$GH_TOKEN@github.com/o/r.git"
            )
        ),
        "in a URL",
    ),
    "the encoded credential shown before it is masked": (
        lambda d: _step_with(d, "git push").update(
            run=_step_with(d, "git push")["run"].replace(
                'echo "::add-mask::$basic"', 'echo "$basic"\n  echo "::add-mask::$basic"'
            )
        ),
        "used before it is masked",
    ),
    "the log showing the credential": (
        lambda d: _step_with(d, "git push").update(
            run="set -x\n" + _step_with(d, "git push")["run"]
        ),
        "would echo the credential",
    ),
    "the encoded credential not masked": (
        lambda d: _step_with(d, "git push").update(
            run=_step_with(d, "git push")["run"].replace("::add-mask::", "::notice::")
        ),
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
