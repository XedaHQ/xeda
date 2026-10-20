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
