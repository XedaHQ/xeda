"""The errors of a remote run. Each names the step that failed and the cause."""

from ..utils import XedaException


class RemoteError(XedaException):
    """A remote run failed before, during or after the launch on the remote."""


class RemoteRefused(RemoteError):
    """The host refused the request before any node started. It is a request error that the
    local planning did not find, for example a setting that only the host's flows reject.

    `code`, `message` and `node` are those of the error in the document of the host."""

    def __init__(self, code: str, message: str, node: str | None) -> None:
        super().__init__(message)
        self.code = code
        self.node = node


class RemoteConfigError(RemoteError):
    """The host profile file or one of its entries is invalid."""


class RemoteConnectionError(RemoteError):
    """OpenSSH could not connect or authenticate, or the connection closed."""


class RemoteProvisionError(RemoteError):
    """The remote environment could not be made. `code` names the failed step, and the message
    starts with it (`<code>: <text>`), so that a user sees it in the report of a failure."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"{code}: {message}")
        self.code = code


class RemoteDependencyError(RemoteError):
    """A local dependency cannot be installed on the remote as it is installed here."""


class RemoteProtocolError(RemoteError):
    """The agent or the bootstrap script sent something that is not in the protocol."""


class RemoteIncompatible(RemoteError):
    """The remote resolved another plan than the local xeda did."""


class RemoteCanceled(RemoteError):
    """The launch on the remote was canceled: by Ctrl-C, or because the connection was lost."""


class RemoteDetached(RemoteError):
    """The local xeda stopped to follow a launch that keeps running (Ctrl-C), or lost the
    connection to it. The launch `launch_id` keeps running on the host."""

    def __init__(self, message: str, launch_id: str, host: str) -> None:
        super().__init__(message)
        self.launch_id = launch_id
        self.host = host


class RemoteInterrupted(RemoteError):
    """Ctrl-C ended a remote run after its launch had finished: while the results were fetched or
    delivered. The results are still on the remote host."""


class RemoteFetchError(RemoteError):
    """A file of the remote run could not be fetched or did not match its digest."""
