"""The private settings files of one user and of one checkout.

Two files hold settings that nothing checks in: the user file, `$XDG_CONFIG_HOME/xeda/config.yaml`,
and the project-local file beside a project file (`xedaproject.local.yaml` beside
`xedaproject.yaml`). Both have the same keys:

- `flows`: a `flows` table, as a project file has it. The user file gives defaults for every run
  of this user. The project-local file overrides the checked-in files of one checkout.
- `env`: environment variables, as text.
- `run_root`: the directory of the run directories. A relative path is relative to the file.

The library never looks for these files. `local_settings()` reads them, as the command line does,
and a caller hands the result to the launcher.
"""

from __future__ import annotations

import difflib
import os
from collections.abc import Mapping
from dataclasses import KW_ONLY, dataclass, field
from pathlib import Path
from typing import Any, Literal

import yaml

from .errors import ProjectError
from .frozen import freeze
from .project import Project
from .remote.errors import RemoteConfigError
from .remote.profile import config_directory, read_host_profiles, remotes_file
from .types import UNSET, PathLike, Unset
from .utils import ConflictingKeys, expand_hierarchy, flows_table_problems
from .yaml_loader import load_yaml, yaml_error_position

__all__ = ["LOCAL_SETTINGS_KEYS", "LocalSettings", "local_settings"]

#: The keys of a private settings file.
LOCAL_SETTINGS_KEYS = ("flows", "env", "run_root")

USER_FILE_NAME = "config.yaml"
LOCAL_FILE_SUFFIX = ".local.yaml"


@dataclass(frozen=True)
class LocalSettings:
    """The private settings of one user and one checkout, as `local_settings()` reads them.

    It is a plain value: a script can build one without files, for example
    `LocalSettings(user={"vivado_synth": {"nthreads": 8}})`. A launcher validates the tables
    when it plans, like the settings of a request."""

    _: KW_ONLY
    #: The `flows` table of the user file.
    user: Mapping[str, Any] = field(default_factory=dict)
    #: The `flows` table of the project-local file.
    local: Mapping[str, Any] = field(default_factory=dict)
    #: The `env` tables of both files; the project-local value wins for a name in both.
    env: Mapping[str, str] = field(default_factory=dict)
    #: The run root that the command line uses without `--run-root` and `XEDA_RUN_ROOT`: the
    #: project-local `run_root`, else the user `run_root`, else the `run_root` that the project
    #: file gives, else `None`. It is an absolute path.
    run_root: Path | None = None
    #: The host profiles, by the text of `--remote`.
    host_profiles: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    #: The files that were read, for the origin of a value and for messages.
    user_file: Path | None = None
    local_file: Path | None = None

    def __post_init__(self) -> None:
        for name in ("user", "local", "env", "host_profiles"):
            object.__setattr__(self, name, freeze(getattr(self, name)))
        if self.run_root is not None:
            object.__setattr__(self, "run_root", Path(self.run_root))


@dataclass(frozen=True)
class _File:
    """What one private file holds."""

    path: Path
    flows: Mapping[str, Any]
    env: Mapping[str, str]
    run_root: Path | None


def local_settings(
    project: Project | None = None, *, config: PathLike | None | Literal[Unset.UNSET] = UNSET
) -> LocalSettings:
    """Read the private settings files and the host profiles, as the command line does.

    Without `config`, the user file is `$XDG_CONFIG_HOME/xeda/config.yaml` (`~/.config/xeda` when
    that variable is empty or relative). With `project`, the project-local file is the one beside
    `project.path`. `config=PATH` reads the file `PATH` in place of the user file; a relative
    `PATH` resolves against the current directory now, once, and a `PATH` that does not exist is an
    error. `config=None` reads no private file. The host profiles are always read, from
    `remotes.yaml` beside the default user file.

    The default user file and the project-local file may be missing: a missing one is no layer.
    Raises `ProjectError` (`invalid_local_settings`) for a file that is malformed, for a `PATH`
    that does not exist, and for malformed host profiles."""
    # The names of the layers are in `settings_layers`, which the launcher imports: import it here
    # so that this module stays below the launcher.
    from .flow_runner.settings_layers import PROJECT_LOCAL_ORIGIN, USER_CONFIG_ORIGIN

    profile_file = remotes_file()
    try:
        profiles = read_host_profiles(profile_file)
    except RemoteConfigError as e:
        raise ProjectError(str(e), code="invalid_local_settings", path=profile_file) from e
    user: _File | None = None
    local: _File | None = None
    if config is not None:
        user_path = (
            config_directory() / USER_FILE_NAME if config is UNSET else Path(config).absolute()
        )
        if config is not UNSET and not user_path.exists():
            raise _invalid(USER_CONFIG_ORIGIN, user_path, "no such file")
        user = _read(user_path, USER_CONFIG_ORIGIN)
        if project is not None:
            local_path = project.path.with_name(project.path.stem + LOCAL_FILE_SUFFIX)
            local = _read(local_path, PROJECT_LOCAL_ORIGIN)
    given = project.run_root if project is not None else None
    run_root = next(
        (file.run_root for file in (local, user) if file is not None and file.run_root is not None),
        given,
    )
    return LocalSettings(
        user=user.flows if user is not None else {},
        local=local.flows if local is not None else {},
        env={**(user.env if user is not None else {}), **(local.env if local is not None else {})},
        run_root=run_root,
        host_profiles=profiles,
        user_file=user.path if user is not None else None,
        local_file=local.path if local is not None else None,
    )


def _invalid(kind: str, path: Path, problem: str) -> ProjectError:
    return ProjectError(
        f'Cannot load {kind} "{path}": {problem}', code="invalid_local_settings", path=path
    )


def _read(path: Path, kind: str) -> _File | None:
    """Return what the private file `path` holds, or None when there is no such file."""
    if not path.exists():
        return None
    if not path.is_file():
        raise _invalid(kind, path, "it is not a file")
    try:
        data = load_yaml(path)
    except yaml.MarkedYAMLError as e:
        reason, line, column = yaml_error_position(e)
        where = f", line {line}, column {column}" if line is not None else ""
        raise _invalid(kind, path, f"{reason}{where}") from e
    except (OSError, ValueError, yaml.YAMLError) as e:
        raise _invalid(kind, path, str(e)) from e
    if data is None:  # an empty file holds nothing
        return _File(path, {}, {}, None)
    if not isinstance(data, dict):
        raise _invalid(kind, path, f"it must hold a mapping, not a {type(data).__name__}")
    try:
        data = expand_hierarchy(data)
    except ConflictingKeys as e:
        raise _invalid(kind, path, str(e)) from e
    unknown = [key for key in data if key not in LOCAL_SETTINGS_KEYS]
    if unknown:
        raise _invalid(kind, path, _unknown_keys(unknown))
    flows = data.get("flows")
    problems = flows_table_problems(flows)
    if problems:
        raise _invalid(kind, path, "; ".join(f"`{key}` {problem}" for key, problem in problems))
    return _File(
        path,
        flows or {},
        _env(kind, path, data.get("env")),
        _run_root(kind, path, data.get("run_root")),
    )


def _unknown_keys(unknown: list[str]) -> str:
    if "designs" in unknown or "design" in unknown:
        return (
            "a private file never declares a design: write the design in a design file or in "
            "the project file"
        )
    hints = [
        f"{key!r}: did you mean {close[0]!r}?"
        for key in unknown
        if (close := difflib.get_close_matches(str(key), LOCAL_SETTINGS_KEYS, n=1))
    ]
    return (
        f"unknown key(s) {', '.join(repr(key) for key in unknown)}; a private file has "
        f"{', '.join(repr(key) for key in LOCAL_SETTINGS_KEYS)}"
        + (f" ({'; '.join(hints)})" if hints else "")
        + ". Write a setting of a flow as flows.<flow>.<setting>."
    )


def _env(kind: str, path: Path, given: Any) -> Mapping[str, str]:
    if given is None:
        return {}
    if not isinstance(given, dict):
        raise _invalid(kind, path, f"`env` is a mapping of names to text, not {given!r:.60}")
    for name, value in given.items():
        if not name or "=" in name or "\0" in name:
            raise _invalid(
                kind, path, f"`env` has the name {name!r}, which is no environment variable name"
            )
        if not isinstance(value, str) or "\0" in value:
            raise _invalid(
                kind, path, f"`env.{name}` is text, not {value!r:.60}; write numbers as '8'"
            )
    return dict(given)


def _run_root(kind: str, path: Path, given: Any) -> Path | None:
    if given is None:
        return None
    if not isinstance(given, str) or not given:
        raise _invalid(kind, path, f"`run_root` is a path, not {given!r:.60}")
    return Path(os.path.normpath(path.parent / given))
