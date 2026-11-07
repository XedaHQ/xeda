"""The tool store: `xeda tools list|path|install` and the code under them.

No test uses the network. A fake GitHub answers the two requests of an install (the release
information and the archive) through `tool_store.open_url`, and the archives are small suites that
the tests build under `tmp_path`: a top directory `oss-cad-suite/` with `bin/yosys`.
"""

from __future__ import annotations

import datetime
import fcntl
import hashlib
import io
import json
import os
import stat
import tarfile
from pathlib import Path
from typing import Any

import pytest
import yaml
from click.testing import CliRunner

import xeda._internal.tool_store as tool_store
from xeda._internal.cli import cli
from xeda._internal.tool_store import (
    ToolInstallError,
    ToolNotInstalled,
    find,
    install,
    installed,
    plan_install,
    select,
)
from .source_layout import IMPL_DIR, IMPL_PREFIX
from .test_schema_baseline import _validate

pytestmark = pytest.mark.python_compat

#: The shipped pin, which one test reads (`test_the_shipped_pin_is_a_date_and_ci_reads_it`). The
#: others run with this fixed pin, so that a bump of the shipped file changes none of them.
SHIPPED_PIN = tool_store.pinned_version
PIN = "2026-10-08"
OLD, NEW = "2026-09-15", "2026-10-20"
PLATFORM = "linux-x64"

#: The platforms and their asset names, as the real release 2026-10-08 lists them.
REAL_NAMES = {
    ("linux", "x86_64"): ("linux-x64", "oss-cad-suite-linux-x64-20261008.tgz"),
    ("linux", "aarch64"): ("linux-arm64", "oss-cad-suite-linux-arm64-20261008.tgz"),
    ("darwin", "x86_64"): ("darwin-x64", "oss-cad-suite-darwin-x64-20261008.tgz"),
    ("darwin", "arm64"): ("darwin-arm64", "oss-cad-suite-darwin-arm64-20261008.tgz"),
}

Member = tuple[str, int, int, bytes, str]  # name, type, mode, data, link target


def dir_(name: str, mode: int = 0o755) -> Member:
    return (name, ord(tarfile.DIRTYPE), mode, b"", "")


def file_(name: str, data: bytes = b"data\n", mode: int = 0o644) -> Member:
    return (name, ord(tarfile.REGTYPE), mode, data, "")


def link_(name: str, target: str) -> Member:
    return (name, ord(tarfile.SYMTYPE), 0o777, b"", target)


def special(name: str, kind: bytes) -> Member:
    return (name, ord(kind), 0o644, b"", "")


GOOD: list[Member] = [
    dir_("oss-cad-suite"),
    dir_("oss-cad-suite/bin"),
    file_("oss-cad-suite/bin/yosys", b"#!/bin/sh\necho yosys\n", 0o755),
    link_("oss-cad-suite/bin/yosys-alias", "yosys"),
    file_("oss-cad-suite/bin/setuid", b"x", 0o4755),
    dir_("oss-cad-suite/lib"),
    file_("oss-cad-suite/lib/libz.so", b"lib\n"),
    link_("oss-cad-suite/lib/libz", "libz.so"),
    dir_("oss-cad-suite/share"),
    link_("oss-cad-suite/share/libz", "../lib/libz.so"),
]


def build_archive(members: list[Member]) -> bytes:
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w:gz") as tar:
        for name, kind, mode, data, target in members:
            info = tarfile.TarInfo(name)
            info.type = bytes([kind])
            info.mode = mode
            info.linkname = target
            info.size = len(data) if kind == ord(tarfile.REGTYPE) else 0
            tar.addfile(info, io.BytesIO(data) if info.size or info.isreg() else None)
    return stream.getvalue()


class Response(io.BytesIO):
    headers: dict[str, str] = {}

    def __enter__(self) -> "Response":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class FakeGitHub:
    """The release API and the downloads of YosysHQ/oss-cad-suite-build."""

    def __init__(self, platform: str = PLATFORM) -> None:
        self.platform = platform
        self.archives: dict[str, bytes] = {}
        self.listed_digest: dict[str, str | None] = {}
        self.calls: list[str] = []
        self.fail: str | None = None
        self.drafts: set[str] = set()

    def add(self, date: str, members: list[Member] | None = None) -> bytes:
        data = build_archive(GOOD if members is None else members)
        self.archives[date] = data
        return data

    def release(self, date: str) -> dict[str, Any]:
        data = self.archives[date]
        name = tool_store.asset_name(date, self.platform)
        digest = self.listed_digest.get(date, "sha256:" + hashlib.sha256(data).hexdigest())
        asset: dict[str, Any] = {"id": 4242, "name": name, "size": len(data)}
        if digest is not None:
            asset["digest"] = digest
        return {"tag_name": date, "draft": date in self.drafts, "assets": [asset]}

    def open_url(self, url: str) -> Response:
        self.calls.append(url)
        if self.fail:
            raise OSError(self.fail)
        api = tool_store.API
        if url == f"{api}/latest":
            return Response(json.dumps(self.release(max(self.archives))).encode())
        if url.startswith(f"{api}/tags/"):
            date = url.removeprefix(f"{api}/tags/")
            if date not in self.archives:
                raise OSError("HTTP Error 404: Not Found")
            return Response(json.dumps(self.release(date)).encode())
        for date, data in self.archives.items():
            if url == f"{tool_store.DOWNLOADS}/{date}/{tool_store.asset_name(date, self.platform)}":
                return Response(data)
        raise OSError(f"HTTP Error 404: {url}")


@pytest.fixture(autouse=True)
def _fixed_pin(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(tool_store, "pinned_version", lambda: PIN)


@pytest.fixture
def github(monkeypatch: pytest.MonkeyPatch) -> FakeGitHub:
    fake = FakeGitHub()
    monkeypatch.setattr(tool_store, "open_url", fake.open_url)
    monkeypatch.setattr(tool_store, "platform_tag", lambda: fake.platform)
    return fake


@pytest.fixture
def root(tmp_path: Path) -> Path:
    return tmp_path / "store"


def install_date(root: Path, version: str = PIN) -> tool_store.Installation:
    return install(plan_install(root, version)).installation


def tree(directory: Path) -> list[str]:
    return sorted(str(p.relative_to(directory)) for p in directory.rglob("*"))


# ------------------------------------------------------------------------------ the platform


@pytest.mark.parametrize("system, machine", sorted(REAL_NAMES))
def test_platform_asset_names(monkeypatch, system, machine):
    """The platform tags and the asset names are those of the real release."""
    monkeypatch.setattr("sys.platform", system)
    monkeypatch.setattr("platform.machine", lambda: machine)
    tag, name = REAL_NAMES[(system, machine)]
    assert tool_store.platform_tag() == tag
    assert tool_store.asset_name("2026-10-08", tag) == name


def test_a_machine_without_a_build_has_no_platform(monkeypatch, root):
    monkeypatch.setattr("sys.platform", "freebsd14")
    assert tool_store.platform_tag() is None
    assert installed(root) == []
    with pytest.raises(ToolInstallError) as raised:
        plan_install(root, PIN)
    assert raised.value.code == "tool_platform_unsupported"


def test_the_default_root(monkeypatch, tmp_path):
    monkeypatch.setattr("sys.platform", "linux")
    home = tmp_path / "home"
    monkeypatch.setattr(Path, "home", lambda: home)
    default = home / ".local" / "share" / "xeda" / "tools"
    assert tool_store.default_root({}) == default
    assert tool_store.default_root({"XDG_DATA_HOME": "relative"}) == default
    assert tool_store.default_root({"XDG_DATA_HOME": str(tmp_path / "x")}) == (
        tmp_path / "x" / "xeda" / "tools"
    )
    assert tool_store.default_root({"XEDA_TOOLS_ROOT": str(tmp_path / "r")}) == tmp_path / "r"
    monkeypatch.setattr("sys.platform", "darwin")
    assert tool_store.default_root({"XDG_DATA_HOME": str(tmp_path / "x")}) == (
        home / "Library" / "Application Support" / "xeda" / "tools"
    )
    with pytest.raises(Exception, match="absolute") as raised:
        tool_store.default_root({"XEDA_TOOLS_ROOT": "relative"})
    assert raised.value.code == "invalid_value"  # type: ignore[attr-defined]


# ------------------------------------------------------------------------------ the release


def test_a_release_without_the_archive_of_this_platform_is_refused(github, root, monkeypatch):
    """No other architecture takes the place of the missing one."""
    github.add(PIN)
    release = github.release

    def other_architecture(date: str) -> dict[str, Any]:
        document = release(date)
        document["assets"][0]["name"] = tool_store.asset_name(date, "linux-arm64")
        return document

    monkeypatch.setattr(github, "release", other_architecture)
    with pytest.raises(ToolInstallError) as raised:
        plan_install(root, PIN)
    assert raised.value.code == "tool_platform_unsupported"
    assert "oss-cad-suite-linux-x64-" in str(raised.value)


def test_a_release_without_a_digest_is_refused(github, root):
    github.add(PIN)
    github.listed_digest[PIN] = None
    with pytest.raises(ToolInstallError) as raised:
        plan_install(root, PIN)
    assert raised.value.code == "tool_checksum_failed"


def test_a_draft_or_unknown_release_is_refused(github, root):
    github.add(PIN)
    github.drafts.add(PIN)
    for version in (PIN, OLD):
        with pytest.raises(ToolInstallError) as raised:
            plan_install(root, version)
        assert raised.value.code == "tool_download_failed"


def test_latest_resolves_once_and_is_never_looked_up_otherwise(github, root):
    github.add(OLD)
    github.add(NEW)
    plan = plan_install(root, "latest")
    assert plan.version == NEW
    assert github.calls == [f"{tool_store.API}/latest"]
    install(plan)
    assert github.calls.count(f"{tool_store.API}/latest") == 1
    github.calls.clear()
    # Looking at the store, selecting and listing use no network.
    assert [i.version for i in installed(root)] == [NEW]
    assert select(root, None).version == NEW  # type: ignore[union-attr]
    assert find(root, NEW) is not None
    assert github.calls == []


def test_the_pin_is_the_default_version(github, root):
    github.add(PIN)
    plan = plan_install(root, None)
    assert plan.version == PIN
    assert plan.asset.url == (
        f"https://github.com/YosysHQ/oss-cad-suite-build/releases/download/{PIN}/"
        f"oss-cad-suite-linux-x64-{PIN.replace('-', '')}.tgz"
    )


# ------------------------------------------------------------------------------ installing


def test_an_install_publishes_the_suite_and_the_receipt(github, root):
    data = github.add(PIN)
    result = install(plan_install(root, PIN))
    installation = result.installation
    assert not result.already_installed
    location = root.resolve() / "oss-cad-suite" / PIN / PLATFORM
    assert installation.location == location
    assert installation.bin == location / "oss-cad-suite" / "bin"
    receipt = json.loads((location / "receipt.json").read_text())
    assert receipt["sha256"] == hashlib.sha256(data).hexdigest()
    assert receipt["asset_id"] == 4242 and receipt["platform"] == PLATFORM
    assert receipt["version"] == PIN and receipt["tool"] == "oss-cad-suite"
    assert receipt["url"] == github.calls[-1]
    # Internal links stay links, the executable bit stays, no privilege bit is set.
    suite = location / "oss-cad-suite"
    assert os.readlink(suite / "bin" / "yosys-alias") == "yosys"
    assert os.readlink(suite / "share" / "libz") == "../lib/libz.so"
    assert (suite / "bin" / "yosys").stat().st_mode & 0o111
    assert not (suite / "lib" / "libz.so").stat().st_mode & 0o111
    assert stat.S_IMODE((suite / "bin" / "setuid").stat().st_mode) == 0o755
    # Nothing else is left in the store.
    assert sorted(p.name for p in root.iterdir()) == [".lock", "oss-cad-suite"]


def test_a_second_install_downloads_nothing(github, root):
    github.add(PIN)
    install_date(root)
    github.calls.clear()
    again = install(plan_install(root, PIN))
    assert again.already_installed
    assert github.calls == [f"{tool_store.API}/tags/{PIN}"]  # the plan; no archive


def test_the_digest_is_checked_before_a_byte_is_extracted(github, root, monkeypatch):
    github.add(PIN)
    github.listed_digest[PIN] = "sha256:" + "0" * 64
    monkeypatch.setattr(
        tool_store,
        "_extract",
        lambda *a: pytest.fail("extracted an archive that failed its digest"),
    )
    with pytest.raises(ToolInstallError) as raised:
        install(plan_install(root, PIN))
    assert raised.value.code == "tool_checksum_failed"
    assert "0" * 64 in str(raised.value)
    assert not (root / "oss-cad-suite").exists()
    assert [p.name for p in root.iterdir()] == [".lock"]


def test_a_short_download_is_refused(github, root):
    github.add(PIN)
    plan = plan_install(root, PIN)
    github.archives[PIN] = github.archives[PIN][:-5]
    with pytest.raises(ToolInstallError) as raised:
        install(plan)
    assert raised.value.code == "tool_download_failed"


def test_a_network_failure_is_a_download_failure(github, root):
    github.add(PIN)
    plan = plan_install(root, PIN)
    github.fail = "connection reset"
    with pytest.raises(ToolInstallError) as raised:
        install(plan)
    assert raised.value.code == "tool_download_failed"
    assert "connection reset" in str(raised.value)
    with pytest.raises(ToolInstallError) as raised:
        plan_install(root, PIN)
    assert raised.value.code == "tool_download_failed"


def test_a_truncated_http_response_is_a_download_failure(github, root, monkeypatch):
    import http.client

    github.add(PIN)
    plan = plan_install(root, PIN)

    class Truncated(Response):
        def read(self, size: int = -1) -> bytes:
            raise http.client.IncompleteRead(b"ab", 10)

    monkeypatch.setattr(tool_store, "open_url", lambda url: Truncated(b""))
    with pytest.raises(ToolInstallError) as raised:
        install(plan)
    assert raised.value.code == "tool_download_failed"
    with pytest.raises(ToolInstallError) as raised:
        plan_install(root, PIN)
    assert raised.value.code == "tool_download_failed"


BAD_ARCHIVES = {
    "absolute name": [file_("/etc/passwd")],
    "parent directory": [dir_("oss-cad-suite"), file_("oss-cad-suite/../../escape")],
    "another top": [dir_("oss-cad-suite"), file_("other/file")],
    "no top": [file_("file")],
    "absolute link": [dir_("oss-cad-suite"), link_("oss-cad-suite/l", "/etc")],
    "link out of the suite": [dir_("oss-cad-suite"), link_("oss-cad-suite/l", "../outside")],
    "deep link out of the suite": [
        dir_("oss-cad-suite"),
        dir_("oss-cad-suite/a"),
        link_("oss-cad-suite/a/l", "../../x"),
    ],
    "device": [dir_("oss-cad-suite"), special("oss-cad-suite/dev", tarfile.CHRTYPE)],
    "fifo": [dir_("oss-cad-suite"), special("oss-cad-suite/fifo", tarfile.FIFOTYPE)],
    "hard link": [
        dir_("oss-cad-suite"),
        file_("oss-cad-suite/a"),
        (
            "oss-cad-suite/b",
            ord(tarfile.LNKTYPE),
            0o644,
            b"",
            "oss-cad-suite/a",
        ),
    ],
    "a name twice": [dir_("oss-cad-suite"), file_("oss-cad-suite/a"), file_("oss-cad-suite/a")],
    "a directory over a file": [
        dir_("oss-cad-suite"),
        file_("oss-cad-suite/a"),
        dir_("oss-cad-suite/a"),
    ],
    "a write through a link": [
        dir_("oss-cad-suite"),
        dir_("oss-cad-suite/real"),
        link_("oss-cad-suite/alias", "real"),
        file_("oss-cad-suite/alias/escape"),
    ],
    "a file below a file": [
        dir_("oss-cad-suite"),
        file_("oss-cad-suite/a"),
        file_("oss-cad-suite/a/b"),
    ],
}


@pytest.mark.parametrize("name", sorted(BAD_ARCHIVES))
def test_archive_escape_and_internal_links(github, root, tmp_path, name):
    github.add(PIN, [*BAD_ARCHIVES[name]])
    with pytest.raises(ToolInstallError) as raised:
        install(plan_install(root, PIN))
    assert raised.value.code == "tool_archive_invalid", raised.value
    assert not (tmp_path / "escape").exists() and not (root.parent / "escape").exists()
    assert not (root / "oss-cad-suite").exists()
    assert [p.name for p in root.iterdir()] == [".lock"]


def test_chained_links_that_leave_the_suite_are_refused(github, root, tmp_path):
    """Each link stays inside the suite when it is read by its text, and the chain does not."""
    github.add(
        PIN,
        [
            *GOOD,
            link_("oss-cad-suite/lib/up", ".."),
            link_("oss-cad-suite/escape", "lib/up/lib/up/lib/up/../../../../../../outside"),
        ],
    )
    with pytest.raises(ToolInstallError) as raised:
        install(plan_install(root, PIN))
    assert raised.value.code == "tool_archive_invalid"
    assert "escape" in str(raised.value) and "outside the suite" in str(raised.value)
    assert [p.name for p in root.iterdir()] == [".lock"]


def test_a_dangling_link_must_stay_inside_the_suite_too(github, root):
    github.add(PIN, [*GOOD, link_("oss-cad-suite/missing", "nothing/here")])
    assert install(plan_install(root, PIN)).installation.version == PIN
    github.add(
        NEW,
        [*GOOD, link_("oss-cad-suite/lib/up", ".."), link_("oss-cad-suite/x", "lib/up/../../y")],
    )
    with pytest.raises(ToolInstallError) as raised:
        install(plan_install(root, NEW))
    assert raised.value.code == "tool_archive_invalid"


@pytest.mark.parametrize("level", ["tool", "version"])
def test_a_link_that_appears_during_the_download_is_refused_before_anything_is_made(
    github, root, tmp_path, monkeypatch, level
):
    github.add(PIN)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    real = tool_store._download

    def download_then_link(plan, target):
        real(plan, target)
        tool = root / "oss-cad-suite"
        if level == "tool":
            tool.symlink_to(elsewhere)
        else:
            tool.mkdir()
            (tool / PIN).symlink_to(elsewhere)

    monkeypatch.setattr(tool_store, "_download", download_then_link)
    with pytest.raises(ToolInstallError) as raised:
        install(plan_install(root, PIN))
    assert raised.value.code == "tool_install_failed" and "symbolic link" in str(raised.value)
    assert list(elsewhere.iterdir()) == []


def test_an_archive_without_yosys_is_not_a_suite(github, root):
    github.add(PIN, [dir_("oss-cad-suite"), dir_("oss-cad-suite/bin")])
    with pytest.raises(ToolInstallError) as raised:
        install(plan_install(root, PIN))
    assert raised.value.code == "tool_archive_invalid"


def test_a_file_that_is_no_archive_is_refused(github, root):
    github.archives[PIN] = b"not a tar file"
    with pytest.raises(ToolInstallError) as raised:
        install(plan_install(root, PIN))
    assert raised.value.code == "tool_archive_invalid"


def test_the_limits_on_entries_and_bytes(github, root, monkeypatch):
    github.add(PIN)
    monkeypatch.setattr(tool_store, "MAX_ENTRIES", 3)
    with pytest.raises(ToolInstallError) as raised:
        install(plan_install(root, PIN))
    assert raised.value.code == "tool_archive_invalid" and "too many entries" in str(raised.value)
    monkeypatch.setattr(tool_store, "MAX_ENTRIES", 300_000)
    monkeypatch.setattr(tool_store, "MAX_BYTES", 10)
    with pytest.raises(ToolInstallError) as raised:
        install(plan_install(root, PIN))
    assert "too large" in str(raised.value)


def test_install_atomic_and_busy(github, root, monkeypatch):
    github.add(OLD)
    github.add(PIN)
    earlier = install_date(root, OLD)
    before = tree(root)
    # A failure while the new archive is extracted leaves the earlier installation as it was.
    real = tool_store._extract

    def fail_late(archive: Path, payload: Path) -> None:
        real(archive, payload)
        raise ToolInstallError("disk full", "tool_install_failed")

    monkeypatch.setattr(tool_store, "_extract", fail_late)
    with pytest.raises(ToolInstallError):
        install(plan_install(root, PIN))
    assert tree(root) == before
    assert [i.version for i in installed(root)] == [OLD]
    assert earlier.receipt == installed(root)[0].receipt
    monkeypatch.undo()
    # A second install waits for nothing: it fails at once while the lock is held.
    monkeypatch.setattr(tool_store, "open_url", github.open_url)
    monkeypatch.setattr(tool_store, "platform_tag", lambda: PLATFORM)
    with open(root / ".lock", "a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        with pytest.raises(ToolInstallError) as raised:
            install(plan_install(root, PIN))
    assert raised.value.code == "tool_install_busy"
    assert tree(root) == before
    assert install_date(root, PIN).version == PIN


def test_an_incomplete_destination_is_refused(github, root):
    github.add(PIN)
    destination = root / "oss-cad-suite" / PIN / PLATFORM
    (destination / "oss-cad-suite").mkdir(parents=True)
    with pytest.raises(ToolInstallError) as raised:
        install(plan_install(root, PIN))
    assert raised.value.code == "tool_install_failed"
    assert "not a complete installation" in str(raised.value)
    assert find(root, PIN) is None
    assert (destination / "oss-cad-suite").is_dir()


def test_a_link_inside_the_store_is_refused_and_the_root_may_be_one(github, tmp_path, root):
    github.add(PIN)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    root.mkdir()
    (root / "oss-cad-suite").symlink_to(elsewhere)
    with pytest.raises(ToolInstallError) as raised:
        install(plan_install(root, PIN))
    assert raised.value.code == "tool_install_failed" and "symbolic link" in str(raised.value)
    assert list(elsewhere.iterdir()) == []
    # The root itself may be a link.
    link = tmp_path / "link-to-root"
    real = tmp_path / "real-root"
    real.mkdir()
    link.symlink_to(real)
    assert install(plan_install(link, PIN)).installation.location == (
        real / "oss-cad-suite" / PIN / PLATFORM
    )


# ------------------------------------------------------------------------------ finding


def damage(location: Path, how: str) -> None:
    if how == "no receipt":
        (location / "receipt.json").unlink()
    elif how == "bad json":
        (location / "receipt.json").write_text("{")
    elif how == "wrong version":
        document = json.loads((location / "receipt.json").read_text())
        (location / "receipt.json").write_text(json.dumps({**document, "version": OLD}))
    elif how == "wrong schema":
        document = json.loads((location / "receipt.json").read_text())
        (location / "receipt.json").write_text(json.dumps({**document, "schema_version": 2}))
    elif how == "missing key":
        document = json.loads((location / "receipt.json").read_text())
        del document["sha256"]
        (location / "receipt.json").write_text(json.dumps(document))
    elif how == "no yosys":
        (location / "oss-cad-suite" / "bin" / "yosys").unlink()


@pytest.mark.parametrize(
    "how", ["no receipt", "bad json", "wrong version", "wrong schema", "missing key", "no yosys"]
)
def test_only_a_complete_installation_counts(github, root, how):
    github.add(PIN)
    installation = install_date(root)
    assert find(root, PIN) is not None
    damage(installation.location, how)
    assert find(root, PIN) is None and installed(root) == []
    assert select(root, None) is None
    with pytest.raises(ToolNotInstalled) as raised:
        select(root, PIN)
    assert raised.value.code == "tool_not_installed"


def test_newest_exact_and_off_selection(github, root):
    for date in (OLD, PIN, NEW):
        github.add(date)
        install_date(root, date)
    assert [i.version for i in installed(root)] == [OLD, PIN, NEW]
    assert select(root, None).version == NEW  # type: ignore[union-attr]
    assert select(root, PIN).version == PIN  # type: ignore[union-attr]
    assert select(root, "off") is None
    with pytest.raises(ToolNotInstalled, match="2026-01-01"):
        select(root, "2026-01-01")


def test_another_platform_is_not_an_installation(github, root):
    github.add(PIN)
    install_date(root)
    assert installed(root, "darwin-arm64") == []
    assert [i.version for i in installed(root, PLATFORM)] == [PIN]


def test_an_installation_below_a_link_is_not_admitted(github, tmp_path, root):
    github.add(PIN)
    installation = install_date(root)
    moved = tmp_path / "moved"
    installation.location.rename(moved)
    installation.location.symlink_to(moved)
    assert find(root, PIN) is None


# ------------------------------------------------------------------------------ the command


def run_tools(*arguments: str, input: str | None = None, env: dict[str, str] | None = None):
    return CliRunner().invoke(cli, ["tools", *arguments], input=input, env=env)


def failure(result) -> dict[str, Any]:
    document = json.loads(result.stdout)
    assert document["success"] is False
    _validate(document, "failure")
    return document["error"]


def test_tools_documents_and_exit_statuses(github, root, tmp_path):
    github.add(PIN)
    root_arg = ["--root", str(root)]
    # An empty store lists the pin, as not installed.
    result = run_tools("list", *root_arg, "--json")
    assert result.exit_code == 0, result.output
    rows = json.loads(result.stdout)
    _validate(rows, "tools-list")
    assert [(r["version"], r["installed"], r["selected"], r["receipt"]) for r in rows] == [
        (PIN, False, False, None)
    ]
    # `path` fails with a document, and the exit status is 1.
    result = run_tools("path", "oss-cad-suite", *root_arg, "--json")
    assert result.exit_code == 1
    assert failure(result)["code"] == "tool_not_installed"
    assert "xeda tools install" in failure(result)["message"]
    # The install gives one document.
    result = run_tools("install", "oss-cad-suite", *root_arg, "--json")
    assert result.exit_code == 0, result.output
    document = json.loads(result.stdout)
    _validate(document, "tools-install")
    assert document["success"] is True and document["already_installed"] is False
    location = Path(document["directory"]).parent
    _validate(json.loads((location / "receipt.json").read_text()), "tool-receipt")
    assert document["receipt"] == json.loads((location / "receipt.json").read_text())
    # The same install again changes nothing and downloads nothing.
    github.calls.clear()
    result = run_tools("install", "oss-cad-suite", *root_arg, "--json")
    assert result.exit_code == 0 and json.loads(result.stdout)["already_installed"] is True
    assert github.calls == []
    # `list` and `path` agree with the install.
    rows = json.loads(run_tools("list", *root_arg, "--json").stdout)
    _validate(rows, "tools-list")
    assert [(r["version"], r["installed"], r["selected"]) for r in rows] == [(PIN, True, True)]
    result = run_tools("path", "oss-cad-suite", *root_arg, "--json")
    path_document = json.loads(result.stdout)
    _validate(path_document, "tools-path")
    assert path_document == {
        k: v for k, v in document.items() if k not in ("success", "already_installed")
    }
    assert run_tools("path", "oss-cad-suite", *root_arg).stdout.strip() == document["directory"]
    # An exact date that is not installed fails with a document.
    result = run_tools("path", "oss-cad-suite", *root_arg, "--version", OLD, "--json")
    assert result.exit_code == 1 and failure(result)["code"] == "tool_not_installed"
    # A mistake in the command is a usage error.
    for arguments in (
        ["path", "oss-cad-suite", "--version", "latest"],
        ["path", "oss-cad-suite", "--version", "2026-13-45"],
        ["install", "oss-cad-suite", "--version", "tomorrow"],
        ["install", "nothing"],
        ["list", "--config", str(tmp_path / "a"), "--no-config"],
    ):
        assert run_tools(*arguments, *root_arg).exit_code == 2, arguments
    # A relative root in the environment is refused.
    assert run_tools("list", env={"XEDA_TOOLS_ROOT": "relative"}).exit_code == 2


def test_the_root_comes_from_the_option_then_the_variable_then_the_private_env(
    github, tmp_path, monkeypatch
):
    github.add(PIN)
    by_option, by_variable, by_private = (tmp_path / n for n in ("option", "variable", "private"))
    install_date(by_option)
    install_date(by_variable)
    install_date(by_private)
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"env": {"XEDA_TOOLS_ROOT": str(by_private)}}))
    monkeypatch.setenv("XEDA_TOOLS_ROOT", str(by_variable))

    def root_of(*arguments: str) -> Path:
        result = run_tools("path", "oss-cad-suite", "--json", *arguments)
        assert result.exit_code == 0, result.output
        return Path(json.loads(result.stdout)["root"])

    assert root_of("--no-config", "--root", str(by_option)) == by_option.resolve()
    assert root_of("--no-config") == by_variable
    assert root_of("--config", str(config)) == by_private
    assert root_of("--config", str(config), "--root", str(by_option)) == by_option.resolve()


def test_only_the_selected_root_is_checked(github, tmp_path, monkeypatch):
    github.add(PIN)
    a_file = tmp_path / "a-file"
    a_file.write_text("")
    good = tmp_path / "good"
    install_date(good)
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"env": {"XEDA_TOOLS_ROOT": str(good)}}))
    # The variable names a file, and the private env overrides it: the variable is not judged.
    monkeypatch.setenv("XEDA_TOOLS_ROOT", str(a_file))
    result = run_tools("path", "oss-cad-suite", "--json", "--config", str(config))
    assert result.exit_code == 0, result.output
    assert Path(json.loads(result.stdout)["root"]) == good
    # A store that does not exist yet is a store: a first install creates it, through the
    # variable and through the option.
    monkeypatch.setenv("XEDA_TOOLS_ROOT", str(tmp_path / "new" / "one"))
    result = run_tools("install", "oss-cad-suite", "--json", "--no-config")
    assert result.exit_code == 0, result.output
    assert Path(json.loads(result.stdout)["root"]) == tmp_path / "new" / "one"
    result = run_tools(
        "install", "oss-cad-suite", "--json", "--no-config", "--root", str(tmp_path / "x")
    )
    assert result.exit_code == 0, result.output
    assert Path(json.loads(result.stdout)["root"]) == (tmp_path / "x").resolve()


def test_no_config_does_no_project_discovery(github, root, monkeypatch):
    import xeda._internal.xedaproject as xedaproject

    def refuse(*args, **kwargs):
        raise AssertionError("project discovery ran")

    monkeypatch.setattr(xedaproject, "resolve_project_file", refuse)
    result = run_tools("list", "--root", str(root), "--no-config", "--json")
    assert result.exit_code == 0, result.output


def test_the_install_hint_names_a_store_that_is_not_the_default(github, tmp_path):
    spaced = tmp_path / "my store"
    result = run_tools("path", "oss-cad-suite", "--root", str(spaced), "--no-config", "--json")
    assert result.exit_code == 1
    assert f"xeda tools install oss-cad-suite --root '{spaced}'" in failure(result)["message"]


def test_the_private_tools_setting_changes_what_path_and_list_show(github, root, tmp_path):
    for date in (OLD, PIN):
        github.add(date)
        install_date(root, date)
    config = tmp_path / "config.yaml"

    def show(tools: dict[str, str] | None):
        config.write_text(yaml.safe_dump({"tools": tools} if tools else {}))
        arguments = ["--root", str(root), "--config", str(config), "--json"]
        selected = [
            r["version"] for r in json.loads(run_tools("list", *arguments).stdout) if r["selected"]
        ]
        path = run_tools("path", "oss-cad-suite", *arguments)
        return selected, path

    selected, path = show(None)
    assert selected == [PIN] and json.loads(path.stdout)["version"] == PIN
    selected, path = show({"oss-cad-suite": OLD})
    assert selected == [OLD] and json.loads(path.stdout)["version"] == OLD
    selected, path = show({"oss-cad-suite": "off"})
    assert selected == [] and path.exit_code == 1
    assert failure(path)["code"] == "tool_not_installed" and "--version" in failure(path)["message"]
    selected, path = show({"oss-cad-suite": "2026-01-01"})
    assert selected == [] and path.exit_code == 1


@pytest.mark.parametrize(
    "command", [["list"], ["path", "oss-cad-suite"], ["install", "oss-cad-suite"]]
)
def test_wrong_private_settings_are_a_refusal_with_their_own_code(github, root, tmp_path, command):
    """As for `xeda run`: one failure document with the code of the error, or the message alone,
    and exit status 2. Never `internal_error` and never a traceback."""
    config = tmp_path / "config.yaml"
    config.write_text(yaml.safe_dump({"tools": {"oss-cad-suite": "maybe"}}))
    arguments = [*command, "--root", str(root), "--config", str(config)]
    result = run_tools(*arguments, "--json")
    assert result.exit_code == 2, result.output
    error = failure(result)
    assert error["code"] == "invalid_local_settings"
    assert "maybe" in error["message"] and str(config) in error["message"]
    assert "Traceback" not in result.output
    text = run_tools(*arguments)
    assert text.exit_code == 2 and "maybe" in text.output and "Traceback" not in text.output
    assert not text.stdout.strip() or "maybe" not in text.stdout
    # A missing --config file and a missing `path:` directory are refusals too.
    missing = run_tools(*command, "--root", str(root), "--config", str(tmp_path / "none"), "--json")
    assert missing.exit_code == 2 and failure(missing)["code"] == "invalid_local_settings"
    config.write_text(yaml.safe_dump({"path": ["no-such-directory"]}))
    result = run_tools(*arguments, "--json")
    assert result.exit_code == 2 and failure(result)["code"] == "invalid_local_settings"
    assert "path[0]" in failure(result)["message"]


def test_the_text_install_shows_the_plan_and_asks(github, root):
    github.add(PIN)
    declined = run_tools("install", "oss-cad-suite", "--root", str(root), input="n\n")
    assert declined.exit_code == 1
    assert str(root.resolve() / "oss-cad-suite" / PIN / PLATFORM) in declined.stdout
    assert "github.com/YosysHQ/oss-cad-suite-build/releases/download" in declined.stdout
    assert not root.exists()
    accepted = run_tools("install", "oss-cad-suite", "--root", str(root), input="y\n")
    assert accepted.exit_code == 0, accepted.output
    assert "is installed in" in accepted.stdout and "tools:" in accepted.stdout
    assert find(root, PIN) is not None
    github.add(NEW)
    yes = run_tools("install", "oss-cad-suite", "--root", str(root), "--version", "latest", "--yes")
    assert yes.exit_code == 0 and "[y/N]" not in yes.stdout
    assert [i.version for i in installed(root)] == [PIN, NEW]


@pytest.mark.parametrize(
    "setup, code",
    [
        ("digest", "tool_checksum_failed"),
        ("network", "tool_download_failed"),
        ("archive", "tool_archive_invalid"),
    ],
)
def test_a_failed_install_gives_a_failure_document(github, root, setup, code):
    github.add(PIN, [file_("/absolute")] if setup == "archive" else None)
    if setup == "digest":
        github.listed_digest[PIN] = "sha256:" + "1" * 64
    if setup == "network":
        github.fail = "unreachable"
    result = run_tools("install", "oss-cad-suite", "--root", str(root), "--json")
    assert result.exit_code == 1
    assert failure(result)["code"] == code
    assert installed(root) == []


def test_a_busy_store_gives_a_failure_document(github, root):
    github.add(PIN)
    root.mkdir()
    with open(root / ".lock", "a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        result = run_tools("install", "oss-cad-suite", "--root", str(root), "--json")
    assert result.exit_code == 1 and failure(result)["code"] == "tool_install_busy"


def test_an_unsupported_machine_gives_a_failure_document(github, root, monkeypatch):
    github.add(PIN)
    monkeypatch.setattr(tool_store, "platform_tag", lambda: None)
    result = run_tools("install", "oss-cad-suite", "--root", str(root), "--json")
    assert result.exit_code == 1 and failure(result)["code"] == "tool_platform_unsupported"


# ------------------------------------------------------------------------------ the pin and CI


def test_the_shipped_pin_is_a_date_and_ci_reads_it():
    """CI installs the suite that the package pins: the workflow reads `tools.json`, and holds no
    date of its own."""
    root = Path(__file__).resolve().parent.parent
    pin_file = IMPL_DIR / "data" / "tools.json"
    shipped = SHIPPED_PIN()
    assert json.loads(pin_file.read_text()) == {"oss-cad-suite": shipped}
    assert tool_store.DATE.fullmatch(shipped)
    datetime.date.fromisoformat(shipped)
    workflow = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert f"src/{IMPL_PREFIX}/data/tools.json" in workflow
    step = workflow.split("YosysHQ/setup-oss-cad-suite")[1].split("- uses:")[0]
    assert "steps." in step and "outputs" in step and shipped not in step
    assert shipped not in workflow


def test_the_pin_is_in_the_package_data():
    root = Path(__file__).resolve().parent.parent
    text = (root / "pyproject.toml").read_text(encoding="utf-8")
    assert '"tools.json"' in text
