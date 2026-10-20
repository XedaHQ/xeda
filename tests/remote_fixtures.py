"""Fixtures of the tests of remote runs. A test module imports the fixtures that it uses and names
them once at module level (`FIXTURES = (fake_ssh,)`), so that the linter does not read each
argument of a test as a redefinition of the import."""

import os
from pathlib import Path
from typing import Iterator, List

import pytest

from .tool_utils import FAKE_TOOLS_DIR, fake_calls, use_fake_tools


@pytest.fixture
def fake_ssh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Put the fake `ssh` (`tests/fake_tools/fake_ssh.py`, which documents it) first on `PATH`.
    The remote host is this machine. Yields the home directory of the remote user, a new
    directory under `tmp_path`; `ssh_calls` reads what the fake recorded."""
    use_fake_tools(monkeypatch)
    home = tmp_path / "fake-ssh" / "home"
    home.mkdir(parents=True)
    monkeypatch.setenv("XEDA_FAKE_SSH_HOME", str(home))
    monkeypatch.setenv("XEDA_FAKE_SSH_PATH", f"{FAKE_TOOLS_DIR}{os.pathsep}/usr/bin:/bin")
    yield home


def ssh_calls(home: Path) -> List[List[str]]:
    """The arguments of every call of the fake `ssh`, in order, for the remote `home` that the
    `fake_ssh` fixture yielded."""
    return fake_calls(home.parent)
