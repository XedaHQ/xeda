"""Host profiles: the xeda-specific facts of a remote host, read from `remotes.yaml`."""

from pathlib import Path

import pytest

from xeda.remote.errors import RemoteConfigError
from xeda.remote.profile import HostProfile, load_host_profile, remotes_file


def write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def config_home(tmp_path: Path, monkeypatch) -> Path:
    home = tmp_path / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home))
    return home


def test_missing_file_gives_the_default_profile(tmp_path, monkeypatch):
    config_home(tmp_path, monkeypatch)
    assert load_host_profile("venona") == HostProfile()


def test_profile_is_read_by_the_text_of_remote(tmp_path, monkeypatch):
    home = config_home(tmp_path, monkeypatch)
    write(
        home / "xeda" / "remotes.yaml",
        "hosts:\n  venona:\n    root: ~/scratch/xeda\n"
        "    setup: [module load vivado/2025.2]\n    env: {LM_LICENSE_FILE: 2100@lic}\n"
        "    flows: {vivado_synth: {nthreads: 8}}\n",
    )
    p = load_host_profile("venona")
    assert p.root == "~/scratch/xeda"
    assert p.setup == ["module load vivado/2025.2"]
    assert p.env == {"LM_LICENSE_FILE": "2100@lic"}
    assert p.flows == {"vivado_synth": {"nthreads": 8}}
    assert p.python == "managed"
    assert load_host_profile("other") == HostProfile()


def test_the_default_profile_has_the_documented_values():
    p = HostProfile()
    assert p.root is None
    assert p.setup == []
    assert p.env == {}
    assert p.python == "managed"
    assert p.flows == {}


def test_empty_file_gives_the_default_profile(tmp_path, monkeypatch):
    home = config_home(tmp_path, monkeypatch)
    write(home / "xeda" / "remotes.yaml", "")
    assert load_host_profile("venona") == HostProfile()


def test_remotes_file_follows_xdg_config_home(tmp_path, monkeypatch):
    home = config_home(tmp_path, monkeypatch)
    assert remotes_file() == home / "xeda" / "remotes.yaml"


def test_remotes_file_defaults_to_home_config(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    assert remotes_file() == tmp_path / ".config" / "xeda" / "remotes.yaml"


def test_a_relative_xdg_config_home_is_ignored(tmp_path, monkeypatch):
    # The XDG Base Directory specification treats a relative path as invalid.
    monkeypatch.setenv("XDG_CONFIG_HOME", "relative/config")
    monkeypatch.setenv("HOME", str(tmp_path))
    assert remotes_file() == tmp_path / ".config" / "xeda" / "remotes.yaml"


@pytest.mark.parametrize(
    "root",
    ["~/scratch/xeda", "/scratch/xeda", "/opt/xeda-2.0+x/cache", "~/a.b_c/d"],
)
def test_plain_absolute_or_home_roots_are_accepted(root, tmp_path, monkeypatch):
    home = config_home(tmp_path, monkeypatch)
    write(home / "xeda" / "remotes.yaml", f"hosts:\n  venona:\n    root: {root}\n")
    assert load_host_profile("venona").root == root


@pytest.mark.parametrize("root", ["rel/dir", "/a b", "/a$b", '/a"b', "~user/x", "/a`b", "~"])
def test_root_must_be_absolute_or_home_relative_and_plain(root, tmp_path, monkeypatch):
    home = config_home(tmp_path, monkeypatch)
    path = write(home / "xeda" / "remotes.yaml", f"hosts:\n  venona:\n    root: '{root}'\n")
    with pytest.raises(RemoteConfigError) as info:
        load_host_profile("venona")
    message = str(info.value)
    assert str(path) in message
    assert "venona" in message
    assert "root" in message


def test_unknown_key_is_refused(tmp_path, monkeypatch):
    home = config_home(tmp_path, monkeypatch)
    path = write(home / "xeda" / "remotes.yaml", "hosts:\n  venona:\n    rooot: /a\n")
    with pytest.raises(RemoteConfigError) as info:
        load_host_profile("venona")
    message = str(info.value)
    assert str(path) in message
    assert "venona" in message
    assert "rooot" in message


def test_unknown_top_level_key_is_refused(tmp_path, monkeypatch):
    home = config_home(tmp_path, monkeypatch)
    write(home / "xeda" / "remotes.yaml", "hosts: {}\nother: 1\n")
    with pytest.raises(RemoteConfigError, match="other"):
        load_host_profile("venona")


@pytest.mark.parametrize("name", ["1X", "A-B", "A.B", "A B"])
def test_bad_env_name_is_refused(name, tmp_path, monkeypatch):
    home = config_home(tmp_path, monkeypatch)
    path = write(home / "xeda" / "remotes.yaml", f"hosts:\n  venona:\n    env: {{'{name}': x}}\n")
    with pytest.raises(RemoteConfigError) as info:
        load_host_profile("venona")
    message = str(info.value)
    assert str(path) in message
    assert "venona" in message
    assert "env" in message


@pytest.mark.parametrize(
    "name",
    [
        "PYTHONPATH",
        "PYTHONHOME",
        "VIRTUAL_ENV",
        "UV_CACHE_DIR",
        "UV_PYTHON_INSTALL_DIR",
        "UV_NO_CONFIG",
        "XEDA_BOOT_WORK",
        "XEDA_BOOT_",
    ],
)
def test_env_names_that_xeda_sets_are_refused(name, tmp_path, monkeypatch):
    home = config_home(tmp_path, monkeypatch)
    write(home / "xeda" / "remotes.yaml", f"hosts:\n  venona:\n    env: {{{name}: x}}\n")
    with pytest.raises(RemoteConfigError) as info:
        load_host_profile("venona")
    assert f"{name} is set by xeda on the remote; remove it from env" in str(info.value)


def test_good_env_names_are_accepted(tmp_path, monkeypatch):
    home = config_home(tmp_path, monkeypatch)
    write(
        home / "xeda" / "remotes.yaml",
        "hosts:\n  venona:\n    env: {_A: '1', LM_LICENSE_FILE: 2100@lic, x9: ''}\n",
    )
    assert load_host_profile("venona").env == {"_A": "1", "LM_LICENSE_FILE": "2100@lic", "x9": ""}


def test_env_values_must_be_text(tmp_path, monkeypatch):
    home = config_home(tmp_path, monkeypatch)
    write(home / "xeda" / "remotes.yaml", "hosts:\n  venona:\n    env: {JOBS: 8}\n")
    with pytest.raises(RemoteConfigError, match="venona"):
        load_host_profile("venona")


def test_python_is_managed_or_an_absolute_path(tmp_path, monkeypatch):
    home = config_home(tmp_path, monkeypatch)
    write(home / "xeda" / "remotes.yaml", "hosts:\n  venona:\n    python: /usr/bin/python3.12\n")
    assert load_host_profile("venona").python == "/usr/bin/python3.12"


@pytest.mark.parametrize("python", ["python3", "~/bin/python", "/usr/bin/py thon", "/a$b"])
def test_python_must_be_managed_or_a_plain_absolute_path(python, tmp_path, monkeypatch):
    home = config_home(tmp_path, monkeypatch)
    path = write(home / "xeda" / "remotes.yaml", f"hosts:\n  venona:\n    python: '{python}'\n")
    with pytest.raises(RemoteConfigError) as info:
        load_host_profile("venona")
    message = str(info.value)
    assert str(path) in message
    assert "venona" in message
    assert "python" in message


def test_setup_must_be_a_list_of_text(tmp_path, monkeypatch):
    home = config_home(tmp_path, monkeypatch)
    write(home / "xeda" / "remotes.yaml", "hosts:\n  venona:\n    setup: module load vivado\n")
    with pytest.raises(RemoteConfigError, match="setup"):
        load_host_profile("venona")


def test_invalid_yaml_names_the_file(tmp_path, monkeypatch):
    home = config_home(tmp_path, monkeypatch)
    path = write(home / "xeda" / "remotes.yaml", "hosts:\n  venona: [\n")
    with pytest.raises(RemoteConfigError) as info:
        load_host_profile("venona")
    assert str(path) in str(info.value)


def test_duplicate_host_key_is_refused(tmp_path, monkeypatch):
    home = config_home(tmp_path, monkeypatch)
    path = write(
        home / "xeda" / "remotes.yaml",
        "hosts:\n  venona:\n    root: /a\n  venona:\n    root: /b\n",
    )
    with pytest.raises(RemoteConfigError) as info:
        load_host_profile("venona")
    assert str(path) in str(info.value)


def test_a_file_that_is_not_a_mapping_is_refused(tmp_path, monkeypatch):
    home = config_home(tmp_path, monkeypatch)
    path = write(home / "xeda" / "remotes.yaml", "- venona\n")
    with pytest.raises(RemoteConfigError) as info:
        load_host_profile("venona")
    assert str(path) in str(info.value)


def test_every_entry_is_checked_on_each_load(tmp_path, monkeypatch):
    # A broken entry fails the load for every host, so a bad file never goes unnoticed.
    home = config_home(tmp_path, monkeypatch)
    write(
        home / "xeda" / "remotes.yaml",
        "hosts:\n  venona:\n    root: /a\n  other:\n    root: rel\n",
    )
    with pytest.raises(RemoteConfigError, match="other"):
        load_host_profile("venona")
