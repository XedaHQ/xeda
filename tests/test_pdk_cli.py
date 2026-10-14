"""The read-only PDK commands: `xeda list-platforms`, `xeda pdk list` and `xeda pdk path`.

They show which ASIC platforms Xeda knows, which PDK files are installed and where. They start no
process, use no network and write nothing. Each subprocess runs with `HOME` under `tmp_path`, so
the default store is empty and the real `~/.ciel` is never read. A fixture store is built under
`tmp_path` from the residue of the platform, with empty files: finding the files only checks that
they exist.
"""

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any, Optional

import click
import pytest
import yaml
from click.testing import CliRunner

from xeda import pdk_cli
from xeda.cli import cli
from xeda.introspect import provider_status
from xeda.platforms import Residue, load_residue
from xeda.platforms.providers import PROVIDERS

pytestmark = pytest.mark.python_compat

TINY = Path(__file__).parent / "resources" / "platforms" / "tiny"

OTHER_COMMIT = "1111111111111111111111111111111111111111"
PIN = "6b305a712966be72d8908695c04398acdbe273ca"


def run_xeda(*args: str, home: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ, HOME=str(home), COLUMNS="200")
    return subprocess.run(
        [sys.executable, "-m", "xeda", *args],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )


def document(proc: subprocess.CompletedProcess) -> Any:
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError as error:
        pytest.fail(f"stdout was not one JSON document ({error})\n{proc.stdout}\n{proc.stderr}")


def relative_files(residue: Residue) -> list[str]:
    """Every file that finding the platform checks, relative to its directory."""
    token = residue.resolve_corner(None)
    paths = [p for files in residue.files.role_paths(token).values() for p in files]
    if residue.upstream is not None:
        paths += residue.upstream.files
    return paths


def touch_all(directory: Path, residue: Residue) -> None:
    for name in relative_files(residue):
        path = directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("")


def nangate_directory(root: Path, commit: Optional[str] = None) -> Path:
    entry = load_residue("nangate45").providers.orfs
    assert entry is not None
    return root / "xeda" / "orfs" / (commit or entry.version) / entry.directory


@pytest.fixture
def home(tmp_path: Path) -> Path:
    path = tmp_path / "home"
    path.mkdir()
    return path


@pytest.fixture
def store(tmp_path: Path) -> Path:
    """A store that holds nangate45 at the pin of its residue."""
    root = tmp_path / "store"
    touch_all(nangate_directory(root), load_residue("nangate45"))
    return root


# ---------------------------------------------------------------------------------------------
# xeda list-platforms
# ---------------------------------------------------------------------------------------------


def test_list_platforms_lists_nangate45_as_not_installed_in_an_empty_home(home):
    proc = run_xeda("list-platforms", "--json", home=home)
    assert proc.returncode == 0, proc.stderr
    rows = {row["platform"]: row for row in document(proc)}
    assert list(rows) == ["nangate45"]
    entry = load_residue("nangate45").providers.orfs
    assert entry is not None
    assert rows["nangate45"] == {
        "platform": "nangate45",
        "description": "FreePDK45 with the Nangate Open Cell Library",
        "provider": "orfs",
        "version": entry.version,
        "installed": False,
    }
    assert not (home / ".ciel").exists()


def test_list_platforms_says_where_an_installed_platform_is(home):
    root = home / ".ciel"
    touch_all(nangate_directory(root), load_residue("nangate45"))
    row = document(run_xeda("list-platforms", "--json", home=home))[0]
    assert row["installed"] is True
    assert row["directory"] == str(nangate_directory(root))


def test_list_platforms_table_says_how_to_install_what_is_missing(home):
    proc = run_xeda("list-platforms", home=home)
    assert proc.returncode == 0, proc.stderr
    assert "nangate45" in proc.stdout
    assert "xeda pdk install <platform>" in proc.stdout


def test_list_platforms_table_gives_no_install_hint_when_everything_is_installed(home):
    touch_all(nangate_directory(home / ".ciel"), load_residue("nangate45"))
    proc = run_xeda("list-platforms", home=home)
    assert proc.returncode == 0, proc.stderr
    assert "xeda pdk install" not in proc.stdout


# ---------------------------------------------------------------------------------------------
# xeda pdk list
# ---------------------------------------------------------------------------------------------


def listed(home: Path, *args: str) -> dict[tuple[str, str], dict[str, Any]]:
    proc = run_xeda("pdk", "list", "--json", *args, home=home)
    assert proc.returncode == 0, proc.stderr
    return {(row["platform"], row["provider"]): row for row in document(proc)}


def test_pdk_list_gives_every_provider_of_a_platform_with_its_pin(home, store):
    rows = listed(home, "--root", str(store))
    assert set(rows) == {("nangate45", "orfs"), ("nangate45", "directory")}
    entry = load_residue("nangate45").providers.orfs
    assert entry is not None
    orfs = rows["nangate45", "orfs"]
    assert orfs["default"] is True
    assert orfs["version"] == entry.version
    assert orfs["installed"] is True
    assert orfs["directory"] == str(nangate_directory(store))
    assert orfs["other_versions"] == []
    assert "other_versions" not in rows["nangate45", "directory"]
    directory = rows["nangate45", "directory"]
    assert directory["default"] is False
    assert directory["version"] == "unpinned"
    assert directory["installed"] is False
    assert directory["directory"] == str(store / "nangate45")


def test_pdk_list_says_that_the_pin_is_missing_from_an_empty_store(home, tmp_path):
    root = tmp_path / "nothing"
    rows = listed(home, "--root", str(root))
    assert rows["nangate45", "orfs"]["installed"] is False
    assert not root.exists()


def test_pdk_list_defaults_to_the_store_in_home(home):
    touch_all(nangate_directory(home / ".ciel"), load_residue("nangate45"))
    assert listed(home)["nangate45", "orfs"]["installed"] is True


def test_pdk_list_names_the_other_installed_versions_by_path(home, store):
    residue = load_residue("nangate45")
    older = nangate_directory(store, OTHER_COMMIT)
    touch_all(older, residue)
    # A checkout without this platform, and a name that is no commit, are not versions of it.
    (store / "xeda" / "orfs" / ("2" * 40)).mkdir()
    (store / "xeda" / "orfs" / "notes").mkdir()
    orfs = listed(home, "--root", str(store))["nangate45", "orfs"]
    assert orfs["installed"] is True
    assert orfs["other_versions"] == [
        {"version": OTHER_COMMIT, "directory": str(older), "installed": True}
    ]


def test_pdk_list_marks_a_partial_other_version_as_not_installed(home, store):
    partial = nangate_directory(store, OTHER_COMMIT)
    names = relative_files(load_residue("nangate45"))
    for name in names[:-1]:
        (partial / name).parent.mkdir(parents=True, exist_ok=True)
        (partial / name).write_text("")
    orfs = listed(home, "--root", str(store))["nangate45", "orfs"]
    assert orfs["other_versions"] == [
        {"version": OTHER_COMMIT, "directory": str(partial), "installed": False}
    ]


def test_pdk_list_shows_other_versions_when_the_pin_is_missing(home, tmp_path):
    root = tmp_path / "store"
    older = nangate_directory(root, OTHER_COMMIT)
    touch_all(older, load_residue("nangate45"))
    orfs = listed(home, "--root", str(root))["nangate45", "orfs"]
    assert orfs["installed"] is False
    assert orfs["other_versions"] == [
        {"version": OTHER_COMMIT, "directory": str(older), "installed": True}
    ]


def test_pdk_list_table_names_the_root(home, store):
    proc = run_xeda("pdk", "list", "--root", str(store), home=home)
    assert proc.returncode == 0, proc.stderr
    assert "nangate45" in proc.stdout
    assert str(store) in proc.stdout


# ---------------------------------------------------------------------------------------------
# xeda pdk path
# ---------------------------------------------------------------------------------------------


def test_pdk_path_gives_the_directory_and_the_role_files(home, store):
    proc = run_xeda("pdk", "path", "nangate45", "--root", str(store), "--json", home=home)
    assert proc.returncode == 0, proc.stderr
    located = document(proc)
    directory = nangate_directory(store)
    entry = load_residue("nangate45").providers.orfs
    assert entry is not None
    assert located["platform"] == "nangate45"
    assert located["provider"] == "orfs"
    assert located["version"] == entry.version
    assert located["root"] == str(store)
    assert located["directory"] == str(directory)
    assert located["corner"] == "typical"
    assert located["files"]["liberty"] == [
        str(directory / "lib" / "NangateOpenCellLibrary_typical.lib")
    ]
    assert located["files"]["tech_lef"] == [
        str(directory / "lef" / "NangateOpenCellLibrary.tech.lef")
    ]
    assert located["upstream_files"] == [str(directory / "config.mk")]


def test_pdk_path_prints_only_the_directory_in_text_mode(home, store):
    proc = run_xeda("pdk", "path", "nangate45", "--root", str(store), home=home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout == str(nangate_directory(store)) + "\n"


def test_pdk_path_defaults_to_the_store_in_home(home):
    touch_all(nangate_directory(home / ".ciel"), load_residue("nangate45"))
    proc = run_xeda("pdk", "path", "nangate45", home=home)
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(nangate_directory(home / ".ciel"))


def test_pdk_path_takes_a_corner_by_alias(home, store):
    located = document(
        run_xeda(
            "pdk", "path", "nangate45", "--root", str(store), "--corner", "tt", "--json", home=home
        )
    )
    assert located["corner"] == "typical"


def test_pdk_path_takes_a_version_that_is_installed(home, store):
    older = nangate_directory(store, OTHER_COMMIT)
    touch_all(older, load_residue("nangate45"))
    proc = run_xeda(
        "pdk", "path", "nangate45", "--root", str(store), "--version", OTHER_COMMIT, home=home
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == str(older)


def test_a_missing_pdk_fails_with_the_four_line_message_on_stderr(home, tmp_path):
    root = tmp_path / "empty"
    proc = run_xeda("pdk", "path", "nangate45", "--root", str(root), home=home)
    assert proc.returncode != 0
    assert proc.stdout == ""
    lines = proc.stderr.splitlines()
    assert len(lines) == 4, proc.stderr
    assert lines[0].startswith("platform 'nangate45' needs the PDK files of")
    assert lines[1].startswith("  looked in: " + str(nangate_directory(root)))
    assert lines[2].startswith("  install:   xeda pdk install nangate45")
    assert lines[3].startswith("  or name another location:")
    assert not root.exists()


def test_a_missing_pdk_fails_with_the_same_message_in_the_json_error(home, tmp_path):
    root = tmp_path / "empty"
    text = run_xeda("pdk", "path", "nangate45", "--root", str(root), home=home)
    proc = run_xeda("pdk", "path", "nangate45", "--root", str(root), "--json", home=home)
    assert proc.returncode != 0
    error = document(proc)
    assert error["success"] is False
    assert error["error"]["type"] == "PdkNotInstalled"
    assert error["error"]["message"] + "\n" == text.stderr


def test_a_missing_pdk_in_the_default_store_fails_the_same_way(home):
    proc = run_xeda("pdk", "path", "nangate45", "--json", home=home)
    assert proc.returncode != 0
    assert document(proc)["error"]["type"] == "PdkNotInstalled"


def test_an_unknown_platform_fails_naming_the_bundled_ones(home, store):
    proc = run_xeda("pdk", "path", "nangate", "--root", str(store), "--json", home=home)
    assert proc.returncode != 0
    error = document(proc)
    assert error["success"] is False
    assert "nangate" in error["error"]["message"]
    assert "the bundled platforms are: nangate45" in error["error"]["message"]


def test_an_unknown_platform_fails_in_text_mode_too(home, store):
    proc = run_xeda("pdk", "path", "nangate", "--root", str(store), home=home)
    assert proc.returncode != 0
    assert "the bundled platforms are: nangate45" in proc.stderr


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        (["--provider", "ciel"], "not served by provider 'ciel'"),
        (["--corner", "nowhere"], "unknown corner 'nowhere'"),
        (["--version", "main"], "not a full commit hash"),
    ],
)
def test_a_provider_corner_or_version_the_platform_cannot_take_fails_naming_it(
    home, store, extra, needle
):
    proc = run_xeda("pdk", "path", "nangate45", "--root", str(store), *extra, "--json", home=home)
    assert proc.returncode != 0
    error = document(proc)
    assert error["success"] is False
    assert needle in error["error"]["message"]


def test_an_unknown_provider_is_refused_by_the_option(home, store):
    proc = run_xeda("pdk", "path", "nangate45", "--provider", "ftp", "--json", home=home)
    assert proc.returncode != 0
    error = document(proc)
    assert error["success"] is False
    assert "orfs" in error["error"]["message"] and "ciel" in error["error"]["message"]


# ---------------------------------------------------------------------------------------------
# A user platform file
# ---------------------------------------------------------------------------------------------


def write_user_file(path: Path, table: dict[str, Any]) -> Path:
    path.write_text(yaml.safe_dump(table))
    return path


def test_pdk_path_locates_a_complete_user_platform_file(home, tmp_path):
    root = tmp_path / "platforms"
    (root / "tiny" / "lib").mkdir(parents=True)
    (root / "tiny" / "lef").mkdir()
    for name, source in [
        ("lib/tiny.lib", "tiny.lib"),
        ("lef/tiny.tlef", "tiny.tlef"),
        ("lef/tiny.lef", "tiny.lef"),
        ("config.mk", "config.mk"),
    ]:
        (root / "tiny" / name).write_bytes((TINY / source).read_bytes())
    user = write_user_file(
        tmp_path / "tiny.yaml",
        {
            "name": "tiny",
            "providers": {"default": "directory", "directory": {"layout": "orfs"}},
            "upstream": {"adapter": "orfs_config_mk", "files": ["config.mk"]},
            "corners": {"default": "typical"},
            "files": {
                "liberty": {"typical": ["lib/tiny.lib"]},
                "tech_lef": "lef/tiny.tlef",
                "cell_lef": ["lef/tiny.lef"],
            },
        },
    )
    proc = run_xeda(
        "pdk", "path", "--platform-file", str(user), "--root", str(root), "--json", home=home
    )
    assert proc.returncode == 0, proc.stderr
    located = document(proc)
    assert located["platform"] == "tiny"
    assert located["provider"] == "directory"
    assert located["version"] == "unpinned"
    assert located["directory"] == str(root / "tiny")
    assert located["files"]["liberty"] == [str(root / "tiny" / "lib" / "tiny.lib")]


def test_pdk_path_locates_a_user_file_over_the_platform_it_extends(home, tmp_path):
    root = tmp_path / "platforms"
    touch_all(root / "mine", load_residue("nangate45"))
    user = write_user_file(
        tmp_path / "mine.yaml",
        {"name": "mine", "extends": "nangate45", "providers": {"default": "directory"}},
    )
    proc = run_xeda(
        "pdk", "path", "--platform-file", str(user), "--root", str(root), "--json", home=home
    )
    assert proc.returncode == 0, proc.stderr
    located = document(proc)
    assert located["platform"] == "mine"
    assert located["directory"] == str(root / "mine")
    assert located["files"]["liberty"] == [
        str(root / "mine" / "lib" / "NangateOpenCellLibrary_typical.lib")
    ]


def test_a_missing_pdk_of_a_user_file_is_refused_like_a_bundled_one(home, tmp_path):
    user = write_user_file(tmp_path / "mine.yaml", {"name": "mine", "extends": "nangate45"})
    proc = run_xeda(
        "pdk",
        "path",
        "--platform-file",
        str(user),
        "--root",
        str(tmp_path / "none"),
        "--json",
        home=home,
    )
    assert proc.returncode != 0
    assert document(proc)["error"]["type"] == "PdkNotInstalled"
    message = document(proc)["error"]["message"]
    assert "platform 'mine' needs the PDK files" in message
    assert f"install:   xeda pdk install --platform-file {user.resolve()} --root " in message


def mine_in_a_checkout(tmp_path: Path, head: str) -> tuple[Path, Path]:
    """A user file over nangate45 served by a directory, and an ORFS checkout that holds the
    files and whose HEAD says `head`. Return the file and the root (`flow/platforms`)."""
    root = tmp_path / "checkout" / "flow" / "platforms"
    touch_all(root / "mine", load_residue("nangate45"))
    (tmp_path / "checkout" / ".git").mkdir()
    (tmp_path / "checkout" / ".git" / "HEAD").write_text(head + "\n")
    user = write_user_file(
        tmp_path / "mine.yaml",
        {"name": "mine", "extends": "nangate45", "providers": {"default": "directory"}},
    )
    return user, root


def test_pdk_path_accepts_a_checkout_at_the_pinned_version(home, tmp_path):
    user, root = mine_in_a_checkout(tmp_path, PIN)
    proc = run_xeda(
        *["pdk", "path", "--platform-file", str(user), "--root", str(root)],
        *["--version", PIN, "--json"],
        home=home,
    )
    assert proc.returncode == 0, proc.stderr
    assert document(proc)["version"] == PIN


def test_pdk_path_refuses_files_whose_marker_names_another_version(home, tmp_path):
    user, root = mine_in_a_checkout(tmp_path, OTHER_COMMIT)
    arguments = ["pdk", "path", "--platform-file", str(user), "--root", str(root)]
    arguments += ["--version", PIN]
    text = run_xeda(*arguments, home=home)
    assert text.returncode != 0
    assert text.stdout == ""
    assert PIN in text.stderr and OTHER_COMMIT in text.stderr
    proc = run_xeda(*arguments, "--json", home=home)
    assert proc.returncode != 0
    error = document(proc)
    assert error["success"] is False
    assert error["error"]["type"] == "PdkVersionMismatch"
    assert error["error"]["message"] + "\n" == text.stderr


def test_a_store_whose_marker_names_another_version_is_not_installed(tmp_path):
    from .test_pdk_providers import CIEL_HASH, OTHER_HASH, make_store, write

    store = make_store(tmp_path, "ciel")
    status = provider_status(store.residue, "ciel", store.root)
    assert status["installed"] is True and "problem" not in status
    write(store.directory / "SOURCES", f"open_pdks {OTHER_HASH}\n")
    status = provider_status(store.residue, "ciel", store.root)
    assert status["installed"] is False
    assert status["directory"] == str(store.directory)
    assert CIEL_HASH in status["problem"] and OTHER_HASH in status["problem"]


def test_a_user_file_that_is_not_a_platform_fails_naming_the_file(home, tmp_path):
    user = write_user_file(tmp_path / "bad.yaml", {"name": "bad", "nonsense": 1})
    proc = run_xeda("pdk", "path", "--platform-file", str(user), "--json", home=home)
    assert proc.returncode != 0
    error = document(proc)
    assert error["success"] is False
    assert str(user) in error["error"]["message"]


def test_a_user_file_that_extends_an_unknown_platform_fails_naming_the_bundled_ones(home, tmp_path):
    user = write_user_file(tmp_path / "mine.yaml", {"name": "mine", "extends": "nowhere"})
    proc = run_xeda("pdk", "path", "--platform-file", str(user), "--json", home=home)
    assert proc.returncode != 0
    assert "the bundled platforms are: nangate45" in document(proc)["error"]["message"]


def test_a_platform_or_a_platform_file_is_needed_but_not_both(home, tmp_path):
    user = write_user_file(tmp_path / "mine.yaml", {"name": "mine", "extends": "nangate45"})
    neither = run_xeda("pdk", "path", "--json", home=home)
    both = run_xeda("pdk", "path", "nangate45", "--platform-file", str(user), "--json", home=home)
    for proc in (neither, both):
        assert proc.returncode != 0
        error = document(proc)
        assert error["success"] is False
        assert error["error"]["type"] == "UsageError"
        assert "--platform-file" in error["error"]["message"]


# ---------------------------------------------------------------------------------------------
# The shape of the commands
# ---------------------------------------------------------------------------------------------


def test_the_provider_choices_are_the_providers_of_the_store():
    assert pdk_cli.PROVIDER_NAMES == PROVIDERS


def test_the_pdk_commands_read_only_the_environment_variables_they_declare():
    group = cli.commands["pdk"]
    assert isinstance(group, click.Group)
    assert set(group.commands) - {"help"} == {"list", "path"}
    for command in (group.commands[name] for name in ("list", "path")):
        parent = click.Context(group, info_name="pdk", auto_envvar_prefix="XEDA")
        ctx = command.make_context(command.name, [], parent=parent, resilient_parsing=True)
        assert ctx.auto_envvar_prefix is None
        for option in command.params:
            assert not option.envvar, option.name


def test_the_default_root_follows_home_and_not_a_variable_of_xeda(home, store):
    """`XEDA_PDK_PATH_ROOT` and `PDK_ROOT` name nothing: the default store is in `HOME`."""
    env = dict(os.environ, HOME=str(home), XEDA_PDK_PATH_ROOT=str(store), PDK_ROOT=str(store))
    proc = subprocess.run(
        [sys.executable, "-m", "xeda", "pdk", "path", "nangate45", "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode != 0
    assert document(proc)["error"]["type"] == "PdkNotInstalled"


# ---------------------------------------------------------------------------------------------
# No process, no network, no write
# ---------------------------------------------------------------------------------------------

_AUDIT: dict[str, Any] = {}
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND


def _audit(event: str, args: tuple) -> None:
    seen = _AUDIT.get("seen")
    if seen is None:
        return
    if event in ("subprocess.Popen", "socket.connect", "os.mkdir", "os.remove", "os.rename"):
        seen.append(event)
    elif event == "open":
        path, mode, flags = args
        writes = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (
            isinstance(flags, int) and flags & _WRITE_FLAGS
        )
        if writes:
            seen.append(f"open {path} {mode}")


sys.addaudithook(_audit)


def test_the_read_only_commands_start_no_process_and_write_nothing(home, store, monkeypatch):
    monkeypatch.setenv("HOME", str(home))
    commands = [
        ["list-platforms", "--json"],
        ["list-platforms"],
        ["pdk", "list", "--root", str(store), "--json"],
        ["pdk", "list", "--root", str(store / "nothing")],
        ["pdk", "path", "nangate45", "--root", str(store), "--json"],
        ["pdk", "path", "nangate45", "--root", str(store / "nothing"), "--json"],
    ]
    # Import everything the commands need before the hook watches: a first import may write a
    # compiled file.
    for words in commands:
        CliRunner().invoke(cli, words)
    scratch = home / "teeth"
    scratch.mkdir()
    _AUDIT["seen"] = seen = []
    try:
        # The hook sees a write, so an empty list below means something.
        (scratch / "probe").write_text("x")
        assert seen
        seen.clear()
        for words in commands:
            CliRunner().invoke(cli, words)
    finally:
        _AUDIT.pop("seen", None)
    assert seen == []
    assert sorted(path.name for path in home.iterdir()) == ["teeth"]
    assert not (store / "nothing").exists()
