"""The remote host that runs the launches of a workspace.

This module holds the value only. It loads no SSH code, so a script that imports `xeda` pays
nothing for a remote run it does not use. The client is `xeda.remote.client`.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import KW_ONLY, dataclass
from typing import Any

from .frozen import freeze

__all__ = ["Remote"]


@dataclass(frozen=True)
class Remote:
    """A host that runs the launches of a `Workspace`, over SSH.

    `host` is an SSH destination: a name from `~/.ssh/config`, `user@host`, or
    `ssh://user@host:port`. `profile` holds the settings of this host as the host profile of
    `remotes.yaml` has them (the keys `root`, `setup`, `env`, `python` and `flows`). `None`
    means the default profile. Xeda never reads `remotes.yaml` for a `Remote`: the caller passes
    the entry, for example `local_settings().host_profiles.get(host)`. The constructor copies the
    profile and freezes it at every depth.

    With `fetch_all`, a run fetches every file of the run directories, and not only the files
    that the records name. With `keep_running`, a launch that is canceled, or that loses its
    connection, keeps running on the host. Its `Result` then has the outcome `detached`."""

    host: str
    _: KW_ONLY
    profile: Mapping[str, Any] | None = None
    fetch_all: bool = False
    keep_running: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.host, str) or not self.host:
            raise ValueError(f"`host` is the text of an SSH destination, not {self.host!r}")
        if self.profile is not None:
            object.__setattr__(self, "profile", freeze(self.profile))
