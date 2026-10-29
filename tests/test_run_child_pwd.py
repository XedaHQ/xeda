"""A child process that `run_child` starts gets `PWD` set to the directory it runs in."""

import contextlib
import os
import sys

import pytest

from xeda.proc_utils import run_child

from .tool_utils import terminal_output

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="PWD is a POSIX variable")

SCRIPT = (
    "import os, sys; open(sys.argv[1], 'w').write(os.environ.get('PWD', '') + '|' + os.getcwd())"
)


@pytest.fixture(params=["log", "terminal"])
def mode(request):
    with terminal_output() if request.param == "terminal" else contextlib.nullcontext():
        yield request.param


def _seen(tmp_path, cwd, **kwargs):
    note = tmp_path / "note.txt"
    run_child([sys.executable, "-c", SCRIPT, str(note)], cwd=cwd, check=True, **kwargs)
    return note.read_text().split("|")


@pytest.mark.parametrize("env", ["inherited", "wrong", "empty"])
def test_the_child_sees_its_working_directory_as_pwd(tmp_path, monkeypatch, mode, env):
    run = tmp_path / "run"
    run.mkdir()
    monkeypatch.setenv("PWD", str(tmp_path))
    given = {
        "inherited": {},
        "wrong": {"env": {**os.environ, "PWD": str(tmp_path)}},
        "empty": {"env": {}},
    }[env]
    pwd, cwd = _seen(tmp_path, run, **given)
    assert pwd == str(run)
    assert os.path.samefile(cwd, run)


def test_the_environment_of_the_caller_is_not_changed(tmp_path):
    run = tmp_path / "run"
    run.mkdir()
    env = {"PWD": "/elsewhere", "KEPT": "yes"}
    _seen(tmp_path, run, env=env)
    assert env == {"PWD": "/elsewhere", "KEPT": "yes"}


def test_a_directory_given_through_a_link_keeps_the_link_path(tmp_path, mode):
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    pwd, _ = _seen(tmp_path, link, env={})
    assert pwd == str(link)
