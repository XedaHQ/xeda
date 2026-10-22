"""Host library for the lean UART transport (see doc/protocol.md)."""

from .backends import (
    BackendUnavailable,
    FtdiBackend,
    LoopbackBackend,
    SerialBackend,
    Transport,
)
from .device import LinkDevice, OpenFailed, ResponseTruncated, UnknownOutcome
from .protocol import (
    HostConfig,
    HostProtocol,
    LinkError,
    Result,
    SessionClosed,
    State,
    Status,
)

__all__ = [
    "BackendUnavailable",
    "FtdiBackend",
    "HostConfig",
    "HostProtocol",
    "LinkDevice",
    "LinkError",
    "LoopbackBackend",
    "OpenFailed",
    "Result",
    "ResponseTruncated",
    "SerialBackend",
    "SessionClosed",
    "State",
    "Status",
    "Transport",
    "UnknownOutcome",
]
