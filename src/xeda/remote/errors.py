"""The errors of a remote run. Each names the step that failed and the cause."""

from ..utils import XedaException


class RemoteError(XedaException):
    """A remote run failed before, during or after the launch on the remote."""


class RemoteConfigError(RemoteError):
    """The host profile file or one of its entries is invalid."""


class RemoteConnectionError(RemoteError):
    """OpenSSH could not connect or authenticate, or the connection closed."""


class RemoteProvisionError(RemoteError):
    """The remote environment could not be made. `code` names the failed step."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class RemoteDependencyError(RemoteError):
    """A local dependency cannot be installed on the remote as it is installed here."""


class RemoteProtocolError(RemoteError):
    """The agent or the bootstrap script sent something that is not in the protocol."""


class RemoteIncompatible(RemoteError):
    """The remote resolved another plan than the local xeda did."""


class RemoteCanceled(RemoteError):
    """The launch on the remote was canceled: by Ctrl-C, or because the connection was lost."""


class RemoteFetchError(RemoteError):
    """A file of the remote run could not be fetched or did not match its digest."""
