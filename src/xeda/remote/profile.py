"""Host profiles: the xeda-specific facts of each remote host, from `remotes.yaml`.

The profile file is a user-level file, `$XDG_CONFIG_HOME/xeda/remotes.yaml` (default
`~/.config/xeda/remotes.yaml`). Its `hosts` table is keyed by the text of `--remote`. SSH facts
stay in `~/.ssh/config`. A host without an entry has the default profile.
"""

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

import yaml
from pydantic import ValidationError

from ..dataclass import Field, XedaBaseModel, field_validator
from ..yaml_loader import load_yaml, yaml_error_position
from .errors import RemoteConfigError

#: The plain characters of a remote path. Anything else (spaces, `$`, quotes, backquotes) is
#: refused, so that a path never needs shell quoting.
_PLAIN_PATH = r"[A-Za-z0-9._/+-]*"
_ROOT = re.compile(r"(?:~/|/)" + _PLAIN_PATH)
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_PYTHON = re.compile(r"/" + _PLAIN_PATH)


def remotes_file() -> Path:
    """Return the path of the host profile file.

    An empty or relative `XDG_CONFIG_HOME` is ignored, as the XDG Base Directory specification
    requires.
    """
    config_home = os.environ.get("XDG_CONFIG_HOME", "")
    base = Path(config_home) if config_home and os.path.isabs(config_home) else None
    if base is None:
        base = Path.home() / ".config"
    return base / "xeda" / "remotes.yaml"


class HostProfile(XedaBaseModel):
    """The xeda-specific facts of one remote host. A host with no entry has the default profile."""

    root: Optional[str] = Field(
        None,
        description="The remote root of the workspaces and the cache: `~/` or an absolute path "
        "of plain characters. Default: `$XDG_CACHE_HOME/xeda`, else `~/.cache/xeda`.",
    )
    setup: List[str] = Field(
        [],
        description="Shell lines that run before every launch on the host, for example "
        "`module load vivado/2025.2`.",
    )
    env: Dict[str, str] = Field(
        {},
        description="Environment variables that every remote command on the host gets, for "
        "example a license server.",
    )
    python: str = Field(
        "managed",
        description="`managed` to install the local Python version with uv, or the absolute "
        "path of a system Python of the same version.",
    )
    flows: Dict[str, Dict[str, Any]] = Field(
        {},
        description="Settings per flow name that apply only on this host, for example "
        "`vivado_synth: {nthreads: 8}`.",
    )

    @field_validator("root")
    @classmethod
    def validate_root(cls, value):
        if value is None or _ROOT.fullmatch(value):
            return value
        raise ValueError(
            f"must be `~/` or an absolute path of letters, digits and `._/+-`, not {value!r}"
        )

    @field_validator("env")
    @classmethod
    def validate_env(cls, value):
        for name in value:
            if not _ENV_NAME.fullmatch(name):
                raise ValueError(
                    f"environment variable name {name!r} is not a shell name "
                    "(letters, digits and `_`, not starting with a digit)"
                )
        return value

    @field_validator("python")
    @classmethod
    def validate_python(cls, value):
        if value == "managed" or _PYTHON.fullmatch(value):
            return value
        raise ValueError(
            "must be `managed` or an absolute path of letters, digits and `._/+-`, "
            f"not {value!r}"
        )


class RemotesFile(XedaBaseModel):
    """The whole profile file. Its entries are checked on every load."""

    hosts: Dict[str, HostProfile] = Field(
        {},
        description="The profile of each host, keyed by the text of `--remote`.",
    )


def _problems(path: Path, error: ValidationError) -> str:
    parts = []
    for problem in error.errors():
        loc = [str(part) for part in problem["loc"]]
        where = []
        if len(loc) >= 2 and loc[0] == "hosts":
            where.append(f"host {loc[1]!r}")
            loc = loc[2:]
        if loc:
            where.append(f"field {'.'.join(loc)!r}")
        place = ", ".join(where) if where else "the file"
        parts.append(f"{place}: {problem['msg'].removeprefix('Value error, ')}")
    return f'Invalid host profiles in "{path}": ' + "; ".join(parts)


def _read_hosts(path: Path) -> Dict[str, HostProfile]:
    try:
        data = load_yaml(path)
    except FileNotFoundError:
        return {}
    except yaml.MarkedYAMLError as e:
        reason, line, column = yaml_error_position(e)
        where = f", line {line}, column {column}" if line is not None else ""
        raise RemoteConfigError(f'Cannot load host profiles "{path}"{where}: {reason}') from e
    except (yaml.YAMLError, OSError) as e:
        raise RemoteConfigError(f'Cannot load host profiles "{path}": {e}') from e
    if data is None:  # an empty file holds no host
        return {}
    if not isinstance(data, dict):
        raise RemoteConfigError(
            f'Invalid host profiles in "{path}": the file must be a mapping with a `hosts` '
            f"table, not a {type(data).__name__}"
        )
    try:
        return RemotesFile.model_validate(data).hosts
    except ValidationError as e:
        raise RemoteConfigError(_problems(path, e)) from e


def load_host_profile(destination: str) -> HostProfile:
    """Return the profile of the host named by `destination` (the text of `--remote`).

    The whole file is checked, so an invalid entry fails the load for every host.
    """
    hosts = _read_hosts(remotes_file())
    return hosts.get(destination, HostProfile())
