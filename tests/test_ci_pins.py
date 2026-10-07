"""The script that keeps the tool pins of CI current (`.github/scripts/bump_ci_pins.py`).

A pin is a line of a workflow file, and the script finds and replaces it by pattern. These tests
settle four things. The real workflow files hold pins the script reads, so an edit that breaks a
pin line fails here and not on a Monday in CI. A replacement changes the pin and nothing else. A
pin never moves back, and nothing runs when upstream is not newer. What GitHub answers is checked
before it reaches a file.
"""

import importlib.util
import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).parent.parent
SCRIPT = ROOT / ".github" / "scripts" / "bump_ci_pins.py"

_spec = importlib.util.spec_from_file_location("bump_ci_pins", SCRIPT)
assert _spec is not None and _spec.loader is not None
pins = importlib.util.module_from_spec(_spec)
sys.modules["bump_ci_pins"] = pins  # the dataclasses of the script look their module up here
_spec.loader.exec_module(pins)

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


def release(tag: Any, *assets: str, **flags: bool) -> dict[str, Any]:
    return {"tag_name": tag, "assets": [{"name": name} for name in assets], **flags}


def oss_cad(date: str, **flags: bool) -> dict[str, Any]:
    return release(date, f"oss-cad-suite-linux-x64-{date.replace('-', '')}.tgz", **flags)


def bsc(tag: str, **flags: bool) -> dict[str, Any]:
    return release(tag, f"bsc-{tag}-ubuntu-24.04.tar.gz", **flags)


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

    def fetch(self, path: str) -> Any:
        return self.answers[path]

    def digest(self, url: str) -> str:
        self.downloads.append(url)
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
    assert pins.newest_bsc(answer) == "2026.07.1"


def test_bsc_releases_are_ordered_as_numbers_not_as_text():
    assert pins.newest_bsc([bsc("2026.07"), bsc("2026.07.1")]) == "2026.07.1"
    assert pins.newest_bsc([bsc("2025.12.3"), bsc("2025.12.10")]) == "2025.12.10"


@pytest.mark.parametrize("answer", [[], {"message": "rate limit"}, [release("2026.01")]])
def test_no_usable_bsc_release_is_an_error(answer):
    with pytest.raises(pins.PinError):
        pins.newest_bsc(answer)


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
def test_an_installer_head_that_is_not_ahead_of_the_pin_is_no_update(status):
    upstream = newer_upstream(comparison=status)
    assert pins.find_update(pin("installer"), OPENXC7_TEXT, upstream.fetch, upstream.digest) is None


@pytest.mark.parametrize("answer", [{}, [], {"status": "other"}, {"status": None}, "ahead"])
def test_a_comparison_that_is_no_comparison_is_an_error(answer):
    with pytest.raises(pins.PinError, match="comparison"):
        pins.installer_is_ahead(answer)


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
    for text in (title, body, message):
        assert "Co-Authored-By" not in text and "Generated with" not in text
        assert all(line == line.rstrip() for line in text.splitlines())


def test_the_text_names_only_the_tools_that_changed():
    upstream = newer_upstream(bsc_releases=[bsc("2026.07.1")], installer_sha=OLD_COMMIT)
    update = pins.find_update(pin("oss_cad"), CI_TEXT, upstream.fetch, upstream.digest)
    _, body = pins.pull_request_text([update])
    assert "OSS CAD Suite" in body
    assert "bsc" not in body and "installer" not in body and "SHA-256" not in body


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
