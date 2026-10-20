"""The code snapshot, the dependency spec and the keys of the remote environment."""

import dataclasses
import importlib.metadata
import io
import os
import re
import sys
import sysconfig
import tarfile
import types
from pathlib import Path
from typing import Optional

import pytest

from xeda.remote import constants, environment
from xeda.remote.environment import (
    DependencySpec,
    PythonChoice,
    RemotePlatform,
    code_snapshot,
    dependency_key,
    dependency_spec,
    python_choice,
    uv_release,
    write_code_tar,
)
from xeda.remote.errors import RemoteDependencyError, RemoteProvisionError
from xeda.version import __version__

PLATFORM = RemotePlatform(system="Linux", machine="x86_64", libc="glibc", libc_version="2.35")
PYTHON = PythonChoice(request="cpython-3.12", path=None)
SPEC = DependencySpec(requirements=("pydantic>=2",), constraints=("pydantic==2.14.1",))


def make_package(root: Path) -> Path:
    """A small package with bytecode, a dotfile and an executable file."""
    package = root / "xeda"
    (package / "sub" / "__pycache__").mkdir(parents=True)
    (package / "__init__.py").write_text("x = 1\n")
    (package / "sub" / "__init__.py").write_text("")
    (package / "sub" / "data.json").write_text("{}\n")
    (package / "sub" / "run.sh").write_text("#!/bin/sh\n")
    (package / "sub" / "run.sh").chmod(0o755)
    (package / "sub" / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"\0")
    (package / "mod.pyc").write_bytes(b"\0")
    (package / ".DS_Store").write_bytes(b"\0")
    (package / "mod.py~").write_text("old")
    (package / "link.py").symlink_to(package / "__init__.py")
    (package / "linked_dir").symlink_to(package / "sub", target_is_directory=True)
    (package / ".hidden").mkdir()
    (package / ".hidden" / "file.py").write_text("")
    return package


def tar_bytes(snapshot: environment.CodeSnapshot) -> bytes:
    out = io.BytesIO()
    write_code_tar(snapshot, out)
    return out.getvalue()


def test_snapshot_skips_bytecode_dotfiles_backups_and_links(tmp_path: Path) -> None:
    snapshot = code_snapshot(make_package(tmp_path))
    names = [name for name, _ in snapshot.files]
    assert names == [
        "xeda/__init__.py",
        "xeda/sub/__init__.py",
        "xeda/sub/data.json",
        "xeda/sub/run.sh",
    ]
    assert all(path.is_file() for _, path in snapshot.files)
    assert snapshot.version == __version__


def test_snapshot_key_changes_with_content_not_with_mtime(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    key = code_snapshot(package).key
    assert re.fullmatch(r"[0-9a-f]{24}", key)
    os.utime(package / "__init__.py", (1_000_000, 1_000_000))
    assert code_snapshot(package).key == key
    (package / "sub" / "__pycache__" / "other.pyc").write_bytes(b"1")
    assert code_snapshot(package).key == key
    (package / "__init__.py").write_text("x = 2\n")
    changed = code_snapshot(package).key
    assert changed != key
    (package / "extra.py").write_text("")
    assert code_snapshot(package).key != changed


def test_snapshot_key_covers_the_executable_bit(tmp_path: Path) -> None:
    package = make_package(tmp_path)
    key = code_snapshot(package).key
    (package / "sub" / "run.sh").chmod(0o644)
    assert code_snapshot(package).key != key


def test_snapshot_key_covers_the_version(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    package = make_package(tmp_path)
    key = code_snapshot(package).key
    monkeypatch.setattr(environment, "__version__", "9.9.9")
    snapshot = code_snapshot(package)
    assert snapshot.key != key
    assert snapshot.version == "9.9.9"


def test_installed_snapshot_is_made_once() -> None:
    first = code_snapshot()
    assert code_snapshot() is first
    assert any(name == "xeda/__init__.py" for name, _ in first.files)
    assert not any("__pycache__" in name for name, _ in first.files)


def test_code_tar_has_the_package_and_a_dist_info(tmp_path: Path) -> None:
    snapshot = code_snapshot(make_package(tmp_path))
    with tarfile.open(fileobj=io.BytesIO(tar_bytes(snapshot))) as tar:
        names = tar.getnames()
        assert "xeda/__init__.py" in names
        assert f"xeda-{__version__}.dist-info/METADATA" in names
        assert f"xeda-{__version__}.dist-info/INSTALLER" in names
        assert names == sorted(names)
        metadata = tar.extractfile(f"xeda-{__version__}.dist-info/METADATA")
        assert metadata is not None
        assert metadata.read().decode() == (
            f"Metadata-Version: 2.1\nName: xeda\nVersion: {__version__}\n"
        )
        installer = tar.extractfile(f"xeda-{__version__}.dist-info/INSTALLER")
        assert installer is not None
        assert installer.read() == b"xeda-remote\n"
        for member in tar.getmembers():
            assert member.isreg()
            assert (member.uid, member.gid, member.mtime) == (0, 0, 0)
            assert (member.uname, member.gname) == ("", "")
            assert not member.name.startswith("/")
        modes = {m.name: m.mode for m in tar.getmembers()}
    assert modes["xeda/sub/run.sh"] == 0o755
    assert modes["xeda/__init__.py"] == 0o644
    assert modes[f"xeda-{__version__}.dist-info/METADATA"] == 0o644


def test_code_tar_matches_the_key_even_if_a_file_changes_later(tmp_path: Path) -> None:
    package = make_package(tmp_path / "src")
    snapshot = code_snapshot(package)
    (package / "__init__.py").write_text("x = 'edited'\n")
    (package / "sub" / "run.sh").chmod(0o644)
    unpacked = tmp_path / "unpacked"
    with tarfile.open(fileobj=io.BytesIO(tar_bytes(snapshot))) as tar:
        tar.extractall(unpacked, filter="data")
    assert code_snapshot(unpacked / "xeda").key == snapshot.key
    assert (unpacked / "xeda" / "__init__.py").read_text() == "x = 1\n"
    assert code_snapshot(package).key != snapshot.key


def test_code_tar_bytes_are_the_same_for_the_same_snapshot(tmp_path: Path) -> None:
    package = make_package(tmp_path / "a")
    first = tar_bytes(code_snapshot(package))
    os.utime(package / "__init__.py", (5, 5))
    again = tar_bytes(code_snapshot(package))
    copy = make_package(tmp_path / "b")
    assert first == again == tar_bytes(code_snapshot(copy))


def test_dependency_spec_pins_the_local_versions() -> None:
    spec = dependency_spec()
    assert any(r.startswith("pydantic") for r in spec.requirements)
    assert f"pydantic=={importlib.metadata.version('pydantic')}" in spec.constraints
    assert not any(c.startswith("xeda==") for c in spec.constraints)
    # pydantic's own requirements are pinned too
    assert any(c.startswith("pydantic-core==") for c in spec.constraints)
    names = [c.split("==")[0] for c in spec.constraints]
    assert names == sorted(names)
    assert not any("extra ==" in r for r in spec.requirements)


def test_dependency_spec_is_a_value() -> None:
    assert dependency_spec() == dependency_spec()
    assert dependency_spec(["pydantic"]) != dependency_spec()


def test_extra_package_is_a_requirement_and_is_pinned() -> None:
    spec = dependency_spec(["pytest>=8"])
    assert "pytest>=8" in spec.requirements
    assert f"pytest=={importlib.metadata.version('pytest')}" in spec.constraints


def test_extra_package_that_is_not_installed_is_refused() -> None:
    with pytest.raises(RemoteDependencyError, match="no-such-package-xyz"):
        dependency_spec(["no-such-package-xyz>=1"])


class FakeDistribution:
    def __init__(
        self, name: str, version: str, requires: Optional[list[str]] = None, direct: str = ""
    ) -> None:
        self.name, self.version, self.requires, self._direct = name, version, requires, direct

    def read_text(self, filename: str) -> Optional[str]:
        return self._direct if filename == "direct_url.json" and self._direct else None


def fake_world(monkeypatch: pytest.MonkeyPatch, *dists: FakeDistribution) -> None:
    table = {d.name: d for d in dists}
    monkeypatch.setattr(environment, "_distribution", lambda name: table.get(name))
    monkeypatch.setattr(
        importlib.metadata,
        "requires",
        lambda name: ["alpha>=1", "Beta_Pkg", "tool; extra == 'dev'"],
    )


def test_closure_walks_installed_names_and_skips_the_others(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake_world(
        monkeypatch,
        FakeDistribution("alpha", "1.2", ["gamma>=2", "winonly; sys_platform == 'win32'"]),
        FakeDistribution("beta-pkg", "3.0", ["alpha", "xeda", "opt; extra == 'x'"]),
        FakeDistribution("gamma", "2.5"),
        FakeDistribution("opt", "9"),
        FakeDistribution("tool", "7"),
    )
    spec = dependency_spec()
    assert spec.requirements == ("Beta_Pkg", "alpha>=1")
    assert spec.constraints == ("alpha==1.2", "beta-pkg==3.0", "gamma==2.5")


def test_package_behind_a_false_marker_is_pinned_too(monkeypatch: pytest.MonkeyPatch) -> None:
    # The walk does not evaluate markers. uv applies a constraint only to a needed package.
    fake_world(
        monkeypatch,
        FakeDistribution("alpha", "1.2", ['colorama; platform_system == "Windows"']),
        FakeDistribution("beta-pkg", "3.0"),
        FakeDistribution("colorama", "0.4.6"),
    )
    assert "colorama==0.4.6" in dependency_spec().constraints


def test_editable_dependency_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_world(
        monkeypatch,
        FakeDistribution(
            "alpha", "1.2", direct='{"url": "file:///src/alpha", "dir_info": {"editable": true}}'
        ),
        FakeDistribution("beta-pkg", "3.0"),
    )
    with pytest.raises(RemoteDependencyError, match="alpha.*editable.*package index"):
        dependency_spec()


def test_dependency_from_a_url_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_world(
        monkeypatch,
        FakeDistribution("alpha", "1.2"),
        FakeDistribution(
            "beta-pkg", "3.0", direct='{"url": "https://example.com/b.zip", "archive_info": {}}'
        ),
    )
    with pytest.raises(RemoteDependencyError, match="beta-pkg.*URL"):
        dependency_spec()


def local_python(
    monkeypatch: pytest.MonkeyPatch, *, implementation: str = "cpython", gil_disabled: int = 0
) -> None:
    """Make the module see another local Python. The names in the module change, not `sys`."""
    fake_sys = types.SimpleNamespace(
        version_info=types.SimpleNamespace(major=3, minor=13),
        implementation=types.SimpleNamespace(name=implementation),
    )
    fake_sysconfig = types.SimpleNamespace(
        get_config_var=lambda name: gil_disabled if name == "Py_GIL_DISABLED" else None
    )
    monkeypatch.setattr(environment, "sys", fake_sys)
    monkeypatch.setattr(environment, "sysconfig", fake_sysconfig)


def test_python_choice_follows_the_local_python(monkeypatch: pytest.MonkeyPatch) -> None:
    local_python(monkeypatch)
    assert python_choice("managed") == PythonChoice("cpython-3.13", None)
    assert python_choice("/usr/bin/python3.13") == PythonChoice(
        "cpython-3.13", "/usr/bin/python3.13"
    )


def test_python_choice_marks_a_free_threaded_build(monkeypatch: pytest.MonkeyPatch) -> None:
    local_python(monkeypatch, gil_disabled=1)
    assert python_choice("managed").request == "cpython-3.13+freethreaded"


def test_python_choice_of_this_python() -> None:
    choice = python_choice("managed")
    assert choice.request.startswith(f"cpython-{sys.version_info.major}.{sys.version_info.minor}")
    assert choice.request.endswith("+freethreaded") == bool(
        sysconfig.get_config_var("Py_GIL_DISABLED")
    )


def test_python_choice_refuses_another_implementation(monkeypatch: pytest.MonkeyPatch) -> None:
    local_python(monkeypatch, implementation="pypy")
    with pytest.raises(RemoteDependencyError, match="CPython"):
        python_choice("managed")


def test_dependency_key_depends_on_every_input() -> None:
    key = dependency_key(SPEC, PYTHON, PLATFORM)
    assert re.fullmatch(r"[0-9a-f]{24}", key)
    assert dependency_key(SPEC, PYTHON, PLATFORM) == key
    variants = [
        dependency_key(dataclasses.replace(SPEC, requirements=("pydantic>=3",)), PYTHON, PLATFORM),
        dependency_key(
            dataclasses.replace(SPEC, constraints=("pydantic==2.15",)), PYTHON, PLATFORM
        ),
        dependency_key(SPEC, PythonChoice("cpython-3.13", None), PLATFORM),
        dependency_key(SPEC, PythonChoice("cpython-3.12+freethreaded", None), PLATFORM),
        dependency_key(SPEC, PythonChoice("cpython-3.12", "/usr/bin/python3"), PLATFORM),
        dependency_key(SPEC, PYTHON, dataclasses.replace(PLATFORM, machine="aarch64")),
        dependency_key(SPEC, PYTHON, dataclasses.replace(PLATFORM, libc="musl")),
        dependency_key(SPEC, PYTHON, dataclasses.replace(PLATFORM, libc_version="2.28")),
    ]
    assert len({key, *variants}) == len(variants) + 1


def test_dependency_key_depends_on_the_layout_and_uv_versions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = dependency_key(SPEC, PYTHON, PLATFORM)
    monkeypatch.setattr(environment, "LAYOUT", "v2")
    layout_key = dependency_key(SPEC, PYTHON, PLATFORM)
    assert layout_key != key
    monkeypatch.setattr(environment, "UV_VERSION", "0.0.1")
    assert dependency_key(SPEC, PYTHON, PLATFORM) not in (key, layout_key)


def test_uv_releases_cover_four_linux_targets() -> None:
    assert set(constants.UV_RELEASES) == {
        "x86_64-unknown-linux-gnu",
        "aarch64-unknown-linux-gnu",
        "x86_64-unknown-linux-musl",
        "aarch64-unknown-linux-musl",
    }
    for target, (url, sha256) in constants.UV_RELEASES.items():
        assert url == (
            f"https://github.com/astral-sh/uv/releases/download/{constants.UV_VERSION}/"
            f"uv-{target}.tar.gz"
        )
        assert re.fullmatch(r"[0-9a-f]{64}", sha256)
    assert re.fullmatch(r"\d+\.\d+\.\d+", constants.UV_VERSION)


@pytest.mark.parametrize("machine", ["x86_64", "aarch64"])
@pytest.mark.parametrize("libc, suffix", [("glibc", "gnu"), ("musl", "musl")])
def test_uv_release_by_platform(machine: str, libc: str, suffix: str) -> None:
    platform = RemotePlatform("Linux", machine, libc, "1.0")
    assert uv_release(platform) == constants.UV_RELEASES[f"{machine}-unknown-linux-{suffix}"]


def test_uv_release_refuses_other_platforms() -> None:
    with pytest.raises(RemoteProvisionError) as not_linux:
        uv_release(RemotePlatform("Darwin", "arm64", "glibc", "1"))
    assert not_linux.value.code == "not-linux"
    with pytest.raises(RemoteProvisionError) as other:
        uv_release(RemotePlatform("Linux", "riscv64", "glibc", "2.39"))
    assert other.value.code == "unsupported-platform"
    assert "riscv64" in str(other.value)
