"""The fixed values of remote runs, in one place."""

#: The version of the directory layout under the remote root (`R/v1/...`). A change of the layout
#: gets a new version, so that a new xeda never misreads an old directory.
LAYOUT = "v1"

#: Seconds that the SSH master connection stays open after its last client closes.
CONTROL_PERSIST = 60

#: OpenSSH keepalive: the interval in seconds, and how many unanswered probes end the connection.
SERVER_ALIVE = (15, 4)

#: Seconds between two heartbeats that the local xeda sends to the agent.
HEARTBEAT = 5

#: Seconds without a heartbeat after which the agent treats the connection as lost.
LEASE = 30

#: Seconds after SIGINT before SIGTERM, and after SIGTERM before SIGKILL, when a launch stops.
CANCEL_GRACE = (15, 10)

#: The version of uv that xeda installs on a remote host. It is release 0.12.23 of 2026-10-03,
#: the newest release that was at least two weeks old on 2026-10-19.
UV_VERSION = "0.12.23"

#: The uv release archives by target triple: the download URL and the SHA-256 of the archive. The
#: digests come from the `.sha256` files that are published with the release.
UV_RELEASES: dict[str, tuple[str, str]] = {
    "x86_64-unknown-linux-gnu": (
        f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/"
        "uv-x86_64-unknown-linux-gnu.tar.gz",
        "9167d72b3319674b6303c4cbe071854bba13ebdf3d76b1a7cbdc175471fb66d6",
    ),
    "aarch64-unknown-linux-gnu": (
        f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/"
        "uv-aarch64-unknown-linux-gnu.tar.gz",
        "6524bd338177ed50d035d39354e12545e993bbeba2ecbddf0480c5b3a81d313f",
    ),
    "x86_64-unknown-linux-musl": (
        f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/"
        "uv-x86_64-unknown-linux-musl.tar.gz",
        "1cff8783850e794470aadb73f54b749542a511fc57b0ce6468b64bd3852e0ade",
    ),
    "aarch64-unknown-linux-musl": (
        f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/"
        "uv-aarch64-unknown-linux-musl.tar.gz",
        "b536543cc4d50661986b165c76ee8aa9056e4fa332edcd153ff2e98760f9359b",
    ),
}
