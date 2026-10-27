"""The environment of the processes of a flow, and the evidence that it gives.

A launch reads the environment of its caller once, at its start, into an `EnvironmentSnapshot`.
Every node of the launch uses that one snapshot, to judge whether it is up to date, to start its
processes and wherever Xeda itself reads a variable for the flow. A sweep takes one snapshot for
all its points. No flow reads the process environment: this module is the one place of the flow
code that does.

The snapshot has two parts, which stay apart. `inherited` is the environment of the caller.
`private` is the `env` of the private settings files (`LocalSettings.env`). The private part
reaches the processes of every flow in both modes, and the container of a Docker flow too. The
inherited part reaches them with `inherit_env` (the default), or only its essential and locator
variables without it.

The view of a node (`admitted`) is the environment that Xeda reads for the node and that the node's
processes inherit. A process gets `node_environment`: the view, then the `env` setting, then the
variables that the flow sets itself. A container gets `container_environment`.

`environment_evidence` is the digest of what a node inherits, with the sorted names of the
variables in it. A trace holds both, so a changed value makes the node stale, and no value is
stored.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from fnmatch import fnmatchcase
from typing import TYPE_CHECKING, Final

from ..frozen import FrozenDict
from ..utils import semantic_hash

if TYPE_CHECKING:
    from ..flow import Flow

__all__ = [
    "DOCKER_LOCATORS",
    "ESSENTIAL_VARIABLES",
    "SESSION_VARIABLES",
    "EnvironmentSnapshot",
    "admitted",
    "container_environment",
    "environment_evidence",
    "node_environment",
]

#: The variables of a shell session, a terminal or a login, and those of the Xeda command line.
#: They change with every session, so the digest of a node leaves them out.
SESSION_VARIABLES: Final[frozenset[str]] = frozenset(
    {
        "PWD",
        "OLDPWD",
        "SHLVL",
        "_",
        "COLUMNS",
        "LINES",
        "TERM_SESSION_ID",
        "ITERM_SESSION_ID",
        "WINDOWID",
        "TMUX",
        "TMUX_PANE",
        "STY",
        "SSH_AUTH_SOCK",
        "SSH_AGENT_PID",
        "SSH_CLIENT",
        "SSH_CONNECTION",
        "SSH_TTY",
        "XDG_SESSION_ID",
        "XEDA_RUN_ROOT",
        "XEDA_REMOTE",
        "XEDA_LOG_LEVEL",
        "XEDA_DETAILED_LOGS",
    }
)

#: The variables that a process gets without `inherit_env`. A name with `*` is a pattern.
ESSENTIAL_VARIABLES: Final[tuple[str, ...]] = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TMPDIR",
    "TERM",
    "LANG",
    "LC_*",
    "TZ",
    "LD_LIBRARY_PATH",
    "DYLD_LIBRARY_PATH",
)

#: The variables that the `docker` client needs to find its daemon. A flow that runs in Docker
#: has them as locators.
DOCKER_LOCATORS: Final[tuple[str, ...]] = (
    "DOCKER_HOST",
    "DOCKER_CONTEXT",
    "DOCKER_CONFIG",
    "DOCKER_CERT_PATH",
)


@dataclass(frozen=True)
class EnvironmentSnapshot:
    """The environment of one launch (or of one sweep), read once.

    `inherited` is the environment of the caller. `private` is the private `env`. Neither can
    change."""

    inherited: Mapping[str, str]
    private: Mapping[str, str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "inherited", FrozenDict(self.inherited))
        object.__setattr__(self, "private", FrozenDict(self.private))

    @classmethod
    def take(cls, private_env: Mapping[str, str]) -> EnvironmentSnapshot:
        """Read the environment of this process now, with the private `env` of the launch."""
        return cls(dict(os.environ), dict(private_env))


def _matches(name: str, patterns: tuple[str, ...]) -> bool:
    return any(fnmatchcase(name, pattern) for pattern in patterns)


def _locators(settings: Flow.Settings, flow_class: type[Flow]) -> tuple[str, ...]:
    """Return the names, or patterns, that locate the tool or the license of the flow."""
    locators = tuple(flow_class.locator_variables)
    return locators + DOCKER_LOCATORS if settings.docker else locators


def _admitted_inherited(
    snapshot: EnvironmentSnapshot, settings: Flow.Settings, flow_class: type[Flow]
) -> dict[str, str]:
    """Return the variables of the caller that the node's view holds."""
    if settings.inherit_env:
        return dict(snapshot.inherited)
    allowed = (*ESSENTIAL_VARIABLES, *_locators(settings, flow_class))
    return {name: value for name, value in snapshot.inherited.items() if _matches(name, allowed)}


def admitted(
    snapshot: EnvironmentSnapshot, settings: Flow.Settings, flow_class: type[Flow]
) -> FrozenDict:
    """Return the view of a node: the variables that Xeda reads for it and its processes inherit.

    With `inherit_env`, it is the environment of the caller. Without, it is the essential
    variables and the locators of the flow, taken from the caller. The private `env` comes over
    it in both modes."""
    return FrozenDict({**_admitted_inherited(snapshot, settings, flow_class), **snapshot.private})


def node_environment(
    snapshot: EnvironmentSnapshot,
    settings: Flow.Settings,
    flow_class: type[Flow],
    own: Mapping[str, str],
) -> dict[str, str]:
    """Return the environment of a process of the node: its view, then `env`, then `own`.

    `own` are the variables that the flow sets for this process. For a flow that runs in Docker,
    this is the environment of the `docker` client."""
    return {**admitted(snapshot, settings, flow_class), **settings.env, **own}


def container_environment(
    snapshot: EnvironmentSnapshot, settings: Flow.Settings, own: Mapping[str, str]
) -> dict[str, str]:
    """Return the environment of a container: the private `env`, then `env`, then `own`.

    No variable of the caller enters a container."""
    return {**snapshot.private, **settings.env, **own}


def environment_evidence(
    snapshot: EnvironmentSnapshot, settings: Flow.Settings, flow_class: type[Flow]
) -> tuple[str, list[str]]:
    """Return the digest of what the node inherits, and the sorted names of the variables in it.

    The digest has two sections, so a value that moves from the caller to the private `env`
    changes it. The session variables (`SESSION_VARIABLES`) are not in the inherited section.
    A variable that `env` or the flow sets stays in: a flow builds some of them from the value of
    the caller, and a value that nothing reads costs one run at most."""
    inherited = {
        name: value
        for name, value in _admitted_inherited(snapshot, settings, flow_class).items()
        if name not in SESSION_VARIABLES
    }
    private = dict(snapshot.private)
    digest = semantic_hash({"inherited": inherited, "private": private})
    return digest, sorted({*inherited, *private})
