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
from xeda.platforms import Residue, load_residue, locate
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


def every_corner_file(residue: Residue) -> list[str]:
    """Every file that finding the platform checks for any corner, relative to its directory."""
    names: list[str] = []
    for corner in residue.files.liberty:
        names += relative_files_of(residue, corner)
    return list(dict.fromkeys(names))


def relative_files_of(residue: Residue, corner: Optional[str]) -> list[str]:
    paths = [p for files in residue.files.role_paths(residue.resolve_corner(corner)).values()
             for p in files]  # fmt: skip
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


def _receipt(store) -> Path:
    """Where `pdk install` leaves the receipt of the ciel platform in `store`."""
    return store.root / "xeda" / "receipts" / f"{store.residue.name}-{store.version}.json"


def test_the_listing_does_not_count_a_ciel_store_without_a_receipt(tmp_path):
    """The listings and `pdk install` use one rule: `ciel` unpacks straight into the store, so a
    store whose install did not finish has the files and no receipt."""
    from .test_pdk_providers import make_store, write

    store = make_store(tmp_path, "ciel")
    status = provider_status(store.residue, "ciel", store.root)
    assert status["installed"] is False and "problem" not in status
    assert status["directory"] == str(store.directory)
    write(_receipt(store), "{}\n")
    assert provider_status(store.residue, "ciel", store.root)["installed"] is True


def test_a_store_whose_marker_names_another_version_is_not_installed(tmp_path):
    from .test_pdk_providers import CIEL_HASH, OTHER_HASH, make_store, write

    store = make_store(tmp_path, "ciel")
    write(_receipt(store), "{}\n")
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
    assert set(group.commands) - {"help"} == {"list", "path", "install"}
    for command in (group.commands[name] for name in ("list", "path", "install")):
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


# ---------------------------------------------------------------------------------------------
# xeda pdk install
#
# No test uses the network. The `orfs` provider fetches from a repository under `tmp_path`, with
# the real `git`. The `ciel` provider runs a fake `ciel` that is the only program on `PATH`.
# ---------------------------------------------------------------------------------------------

NANGATE = "nangate45"


@pytest.fixture
def quiet_git(monkeypatch):
    """The real `git`, without the configuration of the machine that runs the tests."""
    from .tool_utils import require_git

    require_git()
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", os.devnull)
    monkeypatch.setenv("GIT_CONFIG_NOSYSTEM", "1")


def git(directory: Path, *arguments: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@example.com", *arguments],
        cwd=directory,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


class Upstream:
    """A repository that looks like OpenROAD-flow-scripts: one platform directory with the files of
    the residue, and another directory that a sparse checkout must leave out."""

    def __init__(self, directory: Path, missing: Optional[str] = None) -> None:
        residue = load_residue(NANGATE)
        entry = residue.providers.orfs
        assert entry is not None
        self.platform_directory = entry.directory
        self.directory = directory
        directory.mkdir()
        git(directory, "init", "--quiet", "--initial-branch=main")
        git(directory, "config", "uploadpack.allowFilter", "true")
        git(directory, "config", "uploadpack.allowAnySHA1InWant", "true")
        for name in relative_files(residue):
            if name != missing:
                self.write(f"{entry.directory}/{name}", f"placeholder {name}\n")
        self.write("flow/designs/other/readme.txt", "another directory of the repository\n")
        git(directory, "add", ".")
        git(directory, "commit", "--quiet", "-m", "platform files")
        self.commit = git(directory, "rev-parse", "HEAD")
        self.url = directory.as_uri()

    def write(self, name: str, text: str) -> None:
        path = self.directory / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)


@pytest.fixture
def upstream(tmp_path, quiet_git) -> Upstream:
    return Upstream(tmp_path / "upstream")


def nangate() -> Residue:
    return load_residue(NANGATE)


def tree(root: Path) -> list[str]:
    """Every entry under `root`, relative and sorted, or nothing if `root` does not exist."""
    if not root.exists():
        return []
    return sorted(path.relative_to(root).as_posix() for path in root.rglob("*"))


def install_orfs(root: Path, upstream: Upstream, **keywords: Any):
    from xeda.platforms.providers import install

    return install(nangate(), "orfs", upstream.commit, root, url=upstream.url, **keywords)


def test_an_orfs_install_fetches_only_the_platform_directory_of_the_commit(tmp_path, upstream):
    root = tmp_path / "store"
    result = install_orfs(root, upstream)
    directory = root / "xeda" / "orfs" / upstream.commit / upstream.platform_directory
    assert result.already_installed is False
    assert result.located.directory == directory
    for name in relative_files(nangate()):
        assert (directory / name).read_text() == f"placeholder {name}\n"
    assert not (root / "xeda" / "orfs" / upstream.commit / "flow" / "designs").exists()
    assert [p for p in (root / "xeda").iterdir() if p.name.startswith(".partial")] == []


def test_an_orfs_install_writes_a_receipt(tmp_path, upstream):
    from xeda.platforms.providers import orfs_checkout_directory
    from xeda.version import __version__

    root = tmp_path / "store"
    result = install_orfs(root, upstream)
    assert result.receipt == root / "xeda" / "receipts" / f"{NANGATE}-{upstream.commit}.json"
    receipt = json.loads(result.receipt.read_text())
    assert receipt["platform"] == NANGATE
    assert receipt["provider"] == "orfs"
    assert receipt["version"] == upstream.commit
    assert receipt["root"] == str(root)
    assert receipt["directory"] == str(result.located.directory)
    assert receipt["xeda_version"] == __version__
    assert receipt["time"].endswith("+00:00")
    commands = receipt["commands"]
    work = commands[0][-1]
    assert Path(work).name.startswith(".partial-")
    assert Path(work).parent == root / "xeda"
    assert [command[0] for command in commands] == ["git"] * 5
    assert commands[0][:3] == ["git", "init", "--quiet"]
    assert commands[1] == ["git", "-C", work, "remote", "add", "origin", upstream.url]
    assert commands[3][3:] == [
        *["fetch", "--quiet", "--depth", "1", "--filter=blob:none", "origin", upstream.commit]
    ]
    assert commands[4][3:] == ["checkout", "--quiet", "FETCH_HEAD"]
    # The commands filled the partial directory, which the install moved: it is gone now.
    assert not Path(work).exists()
    assert result.located.directory.is_relative_to(orfs_checkout_directory(root, upstream.commit))


def test_an_orfs_install_that_finds_the_files_runs_nothing_and_writes_nothing(tmp_path):
    root = tmp_path / "store"
    touch_all(nangate_directory(root, OTHER_COMMIT), nangate())
    before = tree(root)
    from xeda.platforms.providers import install

    # The URL leads nowhere: a git command would fail.
    result = install(nangate(), "orfs", OTHER_COMMIT, root, url=(tmp_path / "nowhere").as_uri())
    assert result.already_installed is True
    assert result.receipt is None
    assert tree(root) == before


def test_a_failed_orfs_fetch_leaves_the_store_as_it_was(tmp_path, upstream):
    from xeda.platforms.providers import PdkInstallFailed, install

    root = tmp_path / "store"
    unknown = "3" * 40  # a commit that the repository does not have
    with pytest.raises(PdkInstallFailed) as raised:
        install(nangate(), "orfs", unknown, root, url=upstream.url)
    assert "git" in str(raised.value)
    assert tree(root) == ["xeda", "xeda/.lock"]


def test_an_install_whose_files_are_incomplete_is_removed(tmp_path, quiet_git):
    from xeda.platforms.providers import PdkInstallFailed, install

    damaged = Upstream(tmp_path / "damaged", missing="rcx_patterns.rules")
    root = tmp_path / "store"
    with pytest.raises(PdkInstallFailed) as raised:
        install(nangate(), "orfs", damaged.commit, root, url=damaged.url)
    assert "rcx_patterns.rules" in str(raised.value)
    assert tree(root) == ["xeda", "xeda/.lock"]


def test_a_directory_in_the_way_is_refused_and_left_alone(tmp_path, upstream):
    from xeda.platforms.providers import PdkInstallFailed, orfs_checkout_directory

    root = tmp_path / "store"
    directory = orfs_checkout_directory(root, upstream.commit)
    directory.mkdir(parents=True)
    (directory / "mine.txt").write_text("a file of the user\n")
    before = tree(root)
    with pytest.raises(PdkInstallFailed) as raised:
        install_orfs(root, upstream)
    assert str(directory) in str(raised.value)
    assert (directory / "mine.txt").read_text() == "a file of the user\n"
    assert sorted(set(tree(root)) - set(before)) == ["xeda/.lock"]


def test_a_second_install_into_the_same_root_fails_and_says_who_holds_the_lock(tmp_path, upstream):
    import fcntl

    from xeda.platforms.providers import PdkInstallBusy

    root = tmp_path / "store"
    lock = root / "xeda" / ".lock"
    lock.parent.mkdir(parents=True)
    with open(lock, "a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        with pytest.raises(PdkInstallBusy) as raised:
            install_orfs(root, upstream)
    message = str(raised.value)
    assert str(lock) in message
    assert "another" in message and "again" in message
    assert tree(root) == ["xeda", "xeda/.lock"]
    # Once the lock is free, the same call installs.
    assert install_orfs(root, upstream).already_installed is False


def test_an_install_into_a_directory_that_does_not_exist_makes_it(tmp_path, upstream):
    root = tmp_path / "a" / "b" / "store"
    install_orfs(root, upstream)
    assert (root / "xeda" / "receipts").is_dir()


def test_the_directory_provider_has_nothing_to_install(tmp_path):
    from xeda.platforms.providers import install

    with pytest.raises(ValueError, match="nothing to install"):
        install(nangate(), "directory", None, tmp_path / "store")
    assert not (tmp_path / "store").exists()


# ----------------------------------------------------------------------------- ciel (a fake)

FAKE_CIEL = """#!{python}
import json, os, sys
from pathlib import Path

if sys.argv[1:] == ["--version"]:
    print("ciel {version}")
    sys.exit(0)
with open({record!r}, "a") as record:
    record.write(json.dumps({{"argv": sys.argv[1:], "env": dict(os.environ)}}) + "\\n")
if {fail}:
    print("ciel: cannot fetch")
    sys.exit(3)
arguments = sys.argv[1:]
assert arguments[0] == "fetch", arguments
root = Path(arguments[arguments.index("--pdk-root") + 1])
family = arguments[arguments.index("--pdk-family") + 1]
directory = root / "ciel" / family / "versions" / arguments[-1] / {variant!r}
for name in {files!r}:
    (directory / name).parent.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text("")
"""


class FakeCiel:
    def __init__(
        self,
        directory: Path,
        version: str = "3.0.1",
        fail: bool = False,
        leave_out: Optional[str] = None,
    ) -> None:
        from .test_pdk_providers import tiny_residue

        residue, _names = tiny_residue("ciel")
        entry = residue.providers.ciel
        assert entry is not None
        self.residue = residue
        self.entry = entry
        self.directory = directory
        self.record = directory / "calls.jsonl"
        directory.mkdir()
        program = directory / "ciel"
        program.write_text(
            FAKE_CIEL.format(
                python=sys.executable,
                version=version,
                record=str(self.record),
                fail=fail,
                variant=entry.variant,
                files=[name for name in every_corner_file(residue) if name != leave_out],
            )
        )
        program.chmod(0o755)

    def calls(self) -> list[dict[str, Any]]:
        if not self.record.exists():
            return []
        return [json.loads(line) for line in self.record.read_text().splitlines()]


def fake_ciel(monkeypatch, tmp_path: Path, **keywords: Any) -> FakeCiel:
    """A `ciel` that is the only program on `PATH`, so that neither `uv` nor `pipx` is found."""
    fake = FakeCiel(tmp_path / "fakes", **keywords)
    monkeypatch.setenv("PATH", str(fake.directory))
    return fake


def test_a_ciel_install_runs_ciel_with_a_clean_environment(tmp_path, monkeypatch):
    from xeda.platforms.providers import CIEL_VERSION, install

    fake = fake_ciel(monkeypatch, tmp_path)
    monkeypatch.setenv("PDK_ROOT", "/not/this")
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("CIEL_DATA_SOURCE", "elsewhere")
    monkeypatch.setenv("LANG", "C.UTF-8")
    root = tmp_path / "store"
    result = install(fake.residue, "ciel", None, root)
    entry = fake.entry
    (call,) = fake.calls()
    assert call["argv"] == [
        *["fetch", "--pdk-root", str(root), "--pdk-family", entry.family],
        *["-l", entry.library, entry.version],
    ]
    # macOS adds `__CF_USER_TEXT_ENCODING` to every process it starts.
    assert set(call["env"]) - {"__CF_USER_TEXT_ENCODING"} <= {"PATH", "HOME", "LANG"}
    assert call["env"]["PATH"] == str(fake.directory)
    assert call["env"]["LANG"] == "C.UTF-8"
    assert result.located.directory == (
        root / "ciel" / entry.family / "versions" / entry.version / entry.variant
    )
    receipt = json.loads(result.receipt.read_text())
    assert receipt["provider"] == "ciel"
    assert receipt["version"] == entry.version
    assert receipt["commands"] == [["ciel", *call["argv"]]]
    assert CIEL_VERSION.startswith("3.")


def test_a_ciel_install_verifies_every_corner(tmp_path, monkeypatch):
    from xeda.platforms.providers import PdkInstallFailed, install

    fake = fake_ciel(monkeypatch, tmp_path, leave_out="libs.ref/tinylib/lib/tiny_fast.lib")
    with pytest.raises(PdkInstallFailed) as raised:
        install(fake.residue, "ciel", None, tmp_path / "store")
    assert "tiny_fast.lib" in str(raised.value)


def test_a_ciel_that_is_not_version_3_is_refused_before_it_fetches(tmp_path, monkeypatch):
    from xeda.platforms.providers import PdkInstallFailed, install

    fake = fake_ciel(monkeypatch, tmp_path, version="2.9.0")
    root = tmp_path / "store"
    with pytest.raises(PdkInstallFailed) as raised:
        install(fake.residue, "ciel", None, root)
    assert "3" in str(raised.value) and "2.9.0" in str(raised.value)
    assert fake.calls() == []
    assert tree(root) == ["xeda", "xeda/.lock"]


def test_a_failed_ciel_fetch_is_reported(tmp_path, monkeypatch):
    from xeda.platforms.providers import PdkInstallFailed, install

    fake = fake_ciel(monkeypatch, tmp_path, fail=True)
    with pytest.raises(PdkInstallFailed) as raised:
        install(fake.residue, "ciel", None, tmp_path / "store")
    assert "ciel" in str(raised.value) and "3" in str(raised.value)
    assert not (tmp_path / "store" / "xeda" / "receipts").exists()


# ------------------------------------------------------------------------------ the command


def write_tiny_file(path: Path) -> Path:
    from .test_pdk_providers import tiny_residue

    residue, _names = tiny_residue("ciel")
    path.write_text(yaml.safe_dump(residue.model_dump(mode="json", exclude_none=True)))
    return path


def invoke(monkeypatch, *arguments: str, home: Path, input: Optional[str] = None):
    monkeypatch.setenv("HOME", str(home))
    return CliRunner().invoke(cli, ["pdk", "install", *arguments], input=input)


def local_orfs(monkeypatch, upstream: Upstream) -> None:
    monkeypatch.setattr("xeda.platforms.providers.ORFS_URL", upstream.url)


def test_the_plan_names_the_provider_the_pin_the_size_the_directory_and_the_commands(
    monkeypatch, home, tmp_path, upstream
):
    local_orfs(monkeypatch, upstream)
    root = tmp_path / "store"
    result = invoke(
        monkeypatch, NANGATE, "--root", str(root), "--version", upstream.commit,
        home=home, input="n\n",
    )  # fmt: skip
    text = result.stdout
    assert "orfs" in text
    assert upstream.commit in text
    assert "1.7 MiB" in text
    assert str(nangate_directory(root, upstream.commit)) in text
    assert f"fetch --quiet --depth 1 --filter=blob:none origin {upstream.commit}" in text
    assert upstream.url in text
    assert ".partial-" in text


def test_a_refused_confirmation_installs_nothing(monkeypatch, home, tmp_path, upstream):
    local_orfs(monkeypatch, upstream)
    root = tmp_path / "store"
    result = invoke(
        monkeypatch, NANGATE, "--root", str(root), "--version", upstream.commit,
        home=home, input="n\n",
    )  # fmt: skip
    assert result.exit_code != 0
    assert not root.exists()


def test_an_accepted_confirmation_installs(monkeypatch, home, tmp_path, upstream):
    local_orfs(monkeypatch, upstream)
    root = tmp_path / "store"
    result = invoke(
        monkeypatch, NANGATE, "--root", str(root), "--version", upstream.commit,
        home=home, input="y\n",
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    directory = nangate_directory(root, upstream.commit)
    assert str(directory) in result.stdout
    assert (directory / "config.mk").is_file()


def test_yes_installs_without_a_question(monkeypatch, home, tmp_path, upstream):
    local_orfs(monkeypatch, upstream)
    root = tmp_path / "store"
    result = invoke(
        monkeypatch, NANGATE, "--root", str(root), "--version", upstream.commit, "--yes",
        home=home,
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "[y/N]" not in result.stdout
    assert nangate_directory(root, upstream.commit).is_dir()


def test_json_installs_without_a_question_and_gives_one_document(
    monkeypatch, home, tmp_path, upstream
):
    local_orfs(monkeypatch, upstream)
    root = tmp_path / "store"
    result = invoke(
        monkeypatch, NANGATE, "--root", str(root), "--version", upstream.commit, "--json",
        home=home,
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    answer = json.loads(result.stdout)
    directory = nangate_directory(root, upstream.commit)
    assert answer["success"] is True
    assert answer["platform"] == NANGATE
    assert answer["provider"] == "orfs"
    assert answer["version"] == upstream.commit
    assert answer["directory"] == str(directory)
    assert answer["already_installed"] is False
    receipt = Path(answer["receipt"])
    assert receipt == root / "xeda" / "receipts" / f"{NANGATE}-{upstream.commit}.json"
    assert json.loads(receipt.read_text())["directory"] == str(directory)


def test_json_reports_an_installed_pin_without_running_anything(monkeypatch, home, store):
    result = invoke(monkeypatch, NANGATE, "--root", str(store), "--json", home=home)
    assert result.exit_code == 0, result.output
    answer = json.loads(result.stdout)
    assert answer["success"] is True
    assert answer["already_installed"] is True
    assert answer["receipt"] is None
    assert answer["directory"] == str(nangate_directory(store))


def test_text_reports_an_installed_pin_and_asks_nothing(monkeypatch, home, store):
    result = invoke(monkeypatch, NANGATE, "--root", str(store), home=home)
    assert result.exit_code == 0, result.output
    assert "already installed" in result.stdout
    assert str(nangate_directory(store)) in result.stdout
    assert "[y/N]" not in result.stdout


def test_a_failed_install_gives_a_failure_document_and_leaves_the_store(
    monkeypatch, home, tmp_path, upstream
):
    local_orfs(monkeypatch, upstream)
    root = tmp_path / "store"
    unknown = "4" * 40
    result = invoke(monkeypatch, NANGATE, "--root", str(root), "--version", unknown, "--json",
                    home=home)  # fmt: skip
    assert result.exit_code == 1
    answer = json.loads(result.stdout)
    assert answer["success"] is False
    assert answer["error"]["type"] == "PdkInstallFailed"
    assert unknown in answer["error"]["message"]
    assert tree(root) == ["xeda", "xeda/.lock"]


def test_a_busy_store_fails_with_a_document(monkeypatch, home, tmp_path, upstream):
    import fcntl

    local_orfs(monkeypatch, upstream)
    root = tmp_path / "store"
    (root / "xeda").mkdir(parents=True)
    with open(root / "xeda" / ".lock", "a") as held:
        fcntl.flock(held.fileno(), fcntl.LOCK_EX)
        result = invoke(monkeypatch, NANGATE, "--root", str(root), "--version", upstream.commit,
                        "--json", home=home)  # fmt: skip
    assert result.exit_code == 1
    answer = json.loads(result.stdout)
    assert answer["error"]["type"] == "PdkInstallBusy"
    assert not nangate_directory(root, upstream.commit).exists()


def test_the_tool_output_of_a_json_install_stays_off_stdout(home, tmp_path):
    """A `git` that writes to stdout and fails: stdout still holds exactly one document."""
    fakes = tmp_path / "fakes"
    fakes.mkdir()
    program = fakes / "git"
    program.write_text(f"#!{sys.executable}\nprint('noise from git')\nraise SystemExit(1)\n")
    program.chmod(0o755)
    root = tmp_path / "store"
    env = dict(os.environ, HOME=str(home), PATH=str(fakes) + os.pathsep + os.environ["PATH"])
    proc = subprocess.run(
        [sys.executable, "-m", "xeda", "pdk", "install", NANGATE, "--root", str(root), "--json"],
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert proc.returncode == 1
    assert document(proc)["success"] is False
    assert "noise from git" in proc.stderr
    assert "noise" not in proc.stdout.replace(document(proc)["error"]["message"], "")
    assert tree(root) == ["xeda", "xeda/.lock"]


def test_a_user_platform_file_installs_through_ciel(monkeypatch, home, tmp_path):
    fake = fake_ciel(monkeypatch, tmp_path)
    user = write_tiny_file(tmp_path / "tiny.yaml")
    root = tmp_path / "store"
    result = invoke(monkeypatch, "--platform-file", str(user), "--root", str(root), "--yes",
                    home=home)  # fmt: skip
    assert result.exit_code == 0, result.output
    assert "ciel" in result.stdout and "fetch" in result.stdout
    (call,) = fake.calls()
    assert call["argv"][:2] == ["fetch", "--pdk-root"]
    directory = root / "ciel" / "tinyfam" / "versions" / fake.entry.version / "tinyA"
    assert (directory / "libs.ref" / "tinylib" / "lib" / "tiny.lib").is_file()
    assert (root / "xeda" / "receipts" / f"tiny-{fake.entry.version}.json").is_file()


@pytest.mark.parametrize(
    ("extra", "needle"),
    [
        (["--provider", "directory"], "nothing to install"),
        (["--provider", "ciel"], "not served by provider 'ciel'"),
        (["--version", "main"], "not a full commit hash"),
    ],
)
def test_an_install_the_platform_cannot_take_is_a_usage_error(
    monkeypatch, home, tmp_path, extra, needle
):
    root = tmp_path / "store"
    result = invoke(monkeypatch, NANGATE, "--root", str(root), "--yes", *extra, home=home)
    assert result.exit_code == 2
    assert needle in result.output
    assert not root.exists()


def test_a_missing_installer_is_reported_before_anything_is_written(monkeypatch, home, tmp_path):
    user = write_tiny_file(tmp_path / "tiny.yaml")
    monkeypatch.setenv("PATH", str(tmp_path))  # no uv, no pipx, no ciel
    root = tmp_path / "store"
    result = invoke(monkeypatch, "--platform-file", str(user), "--root", str(root), "--json",
                    home=home)  # fmt: skip
    assert result.exit_code == 1
    assert json.loads(result.stdout)["error"]["type"] == "PdkInstallerMissing"
    assert not root.exists()


def test_the_help_says_that_json_asks_no_question():
    result = CliRunner().invoke(cli, ["pdk", "install", "--help"])
    text = click.unstyle(result.output)
    assert "--yes" in text
    assert "--json" in text
    assert "without a question" in " ".join(text.split())


# ---------------------------------------------------------------------------------------------
# The environment of the install, the errors of the file system and the receipt of ciel
# ---------------------------------------------------------------------------------------------


def repository_state(directory: Path) -> dict[str, str]:
    """Every file under a repository, with its content, to compare before and after."""
    return {
        path.relative_to(directory).as_posix(): path.read_text(errors="replace")
        for path in sorted(directory.rglob("*"))
        if path.is_file() and path.name != "index"
    }


def test_git_runs_in_its_own_repository_whatever_the_caller_sets(tmp_path, upstream, monkeypatch):
    """GIT_DIR, GIT_WORK_TREE and GIT_INDEX_FILE point at another repository: the install still
    fills its own, and leaves the other one as it was."""
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "--quiet")
    (other / "mine.txt").write_text("mine\n")
    git(other, "add", "mine.txt")
    git(other, "commit", "--quiet", "-m", "mine")
    before = repository_state(other)
    head = git(other, "rev-parse", "HEAD")
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))
    monkeypatch.setenv("GIT_WORK_TREE", str(other))
    monkeypatch.setenv("GIT_INDEX_FILE", str(other / ".git" / "index"))
    root = tmp_path / "store"
    result = install_orfs(root, upstream)
    assert result.already_installed is False
    assert (result.located.directory / "config.mk").is_file()
    assert repository_state(other) == before
    monkeypatch.delenv("GIT_DIR")
    monkeypatch.delenv("GIT_WORK_TREE")
    monkeypatch.delenv("GIT_INDEX_FILE")
    assert git(other, "rev-parse", "HEAD") == head
    assert git(other, "status", "--porcelain") == ""


def test_a_hook_of_the_global_configuration_does_not_run(tmp_path, upstream, monkeypatch):
    hooks = tmp_path / "hooks"
    hooks.mkdir()
    marker = tmp_path / "hook-ran"
    hook = hooks / "post-checkout"
    hook.write_text(f"#!/bin/sh\necho ran > {marker}\n")
    hook.chmod(0o755)
    config = tmp_path / "gitconfig"
    config.write_text(f"[core]\n\thooksPath = {hooks}\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    install_orfs(tmp_path / "store", upstream)
    assert not marker.exists()


def test_git_gets_a_controlled_environment(tmp_path, monkeypatch):
    """A `git` that records its environment: only the variables that the install passes."""
    from xeda.platforms.providers import install

    fakes = tmp_path / "fakes"
    fakes.mkdir()
    record = tmp_path / "git-env.json"
    program = fakes / "git"
    program.write_text(
        f"#!{sys.executable}\nimport json, os, sys\n"
        f"open({str(record)!r}, 'w').write(json.dumps({{'env': dict(os.environ), "
        "'argv': sys.argv[1:]}))\nsys.exit(1)\n"
    )
    program.chmod(0o755)
    monkeypatch.setenv("PATH", str(fakes) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv("GITHUB_TOKEN", "secret")
    monkeypatch.setenv("GIT_ASKPASS", "/bin/true")
    monkeypatch.setenv("https_proxy", "http://proxy.example:3128")
    monkeypatch.setenv("NO_PROXY", "example.com")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/elsewhere/gitconfig")
    from xeda.platforms.providers import PdkInstallFailed

    with pytest.raises(PdkInstallFailed):
        install(nangate(), "orfs", PIN, tmp_path / "store")
    seen = json.loads(record.read_text())
    env = seen["env"]
    assert env["GIT_CONFIG_NOSYSTEM"] == "1"
    assert env["GIT_CONFIG_GLOBAL"] == os.devnull
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["https_proxy"] == "http://proxy.example:3128"
    assert env["NO_PROXY"] == "example.com"
    assert "GITHUB_TOKEN" not in env and "GIT_ASKPASS" not in env
    allowed = {"PATH", "HOME", "LANG", "https_proxy", "NO_PROXY", "__CF_USER_TEXT_ENCODING"}
    assert set(env) - allowed <= {"GIT_CONFIG_NOSYSTEM", "GIT_CONFIG_GLOBAL", "GIT_TERMINAL_PROMPT"}
    assert seen["argv"][:2] == ["-c", f"core.hooksPath={os.devnull}"]


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a permission check")
def test_a_root_that_cannot_be_written_fails_with_one_document(monkeypatch, home, tmp_path):
    root = tmp_path / "locked"
    root.mkdir()
    root.chmod(0o555)
    try:
        result = invoke(monkeypatch, NANGATE, "--root", str(root), "--json", home=home)
    finally:
        root.chmod(0o755)
    assert result.exit_code == 1
    answer = json.loads(result.stdout)
    assert answer["success"] is False
    assert answer["error"]["type"] == "PdkInstallFailed"
    assert str(root) in answer["error"]["message"]
    assert tree(root) == []


@pytest.mark.skipif(os.name != "posix" or os.geteuid() == 0, reason="needs a permission check")
def test_a_receipt_that_cannot_be_written_fails_with_a_message(tmp_path, monkeypatch):
    from xeda.platforms import providers

    fake = fake_ciel(monkeypatch, tmp_path)
    root = tmp_path / "store"
    receipts = root / "xeda" / "receipts"
    receipts.mkdir(parents=True)
    receipts.chmod(0o555)
    try:
        with pytest.raises(providers.PdkInstallFailed) as raised:
            providers.install(fake.residue, "ciel", None, root)
    finally:
        receipts.chmod(0o755)
    assert str(receipts) in str(raised.value)


def test_a_ciel_store_without_a_receipt_is_not_installed(tmp_path, monkeypatch):
    """ciel unpacks into the store, so a killed ciel can leave a truncated file that a check of
    the names accepts. Only the receipt, which comes after a verified install, says that it is
    complete."""
    from xeda.platforms.providers import install, installed

    fake = fake_ciel(monkeypatch, tmp_path)
    root = tmp_path / "store"
    entry = fake.entry
    directory = root / "ciel" / entry.family / "versions" / entry.version / entry.variant
    for name in every_corner_file(fake.residue):
        (directory / name).parent.mkdir(parents=True, exist_ok=True)
        (directory / name).write_text("")
    # The names are all there, and finding the files accepts them.
    assert locate(fake.residue, "ciel", None, root, None).directory == directory
    assert installed(fake.residue, "ciel", None, root) is None
    first = install(fake.residue, "ciel", None, root)
    assert first.already_installed is False
    assert len(fake.calls()) == 1
    assert installed(fake.residue, "ciel", None, root) is not None
    second = install(fake.residue, "ciel", None, root)
    assert second.already_installed is True
    assert second.receipt == first.receipt
    assert len(fake.calls()) == 1


def test_an_orfs_store_needs_no_receipt(tmp_path):
    from xeda.platforms.providers import installed

    root = tmp_path / "store"
    touch_all(nangate_directory(root), nangate())
    assert installed(nangate(), "orfs", None, root) is not None


@pytest.mark.parametrize("fmt", ["json", "jsonl", "yaml"])
def test_every_machine_readable_format_installs_without_a_question(
    monkeypatch, home, tmp_path, upstream, fmt
):
    local_orfs(monkeypatch, upstream)
    root = tmp_path / "store"
    result = invoke(
        monkeypatch, NANGATE, "--root", str(root), "--version", upstream.commit,
        *["--format", fmt], home=home,
    )  # fmt: skip
    assert result.exit_code == 0, result.output
    assert nangate_directory(root, upstream.commit).is_dir()
    assert "[y/N]" not in result.stdout + result.stderr


def test_the_help_names_every_format_that_asks_no_question():
    text = " ".join(
        click.unstyle(CliRunner().invoke(cli, ["pdk", "install", "--help"]).output).split()
    )
    text = text.replace("`", "")
    assert "--json, --format json, --format jsonl and --format yaml install without" in text
    assert "without a question" in text
