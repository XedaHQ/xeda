"""Run a request on a remote Linux host over the user's OpenSSH client.

The local xeda checks and plans the request, packages its inputs and opens one SSH master
connection. A shell bootstrap provisions uv, Python, a dependency environment and a snapshot of
the local `xeda` package on the remote. A xeda agent there syncs the inputs, supervises the
launch and sends back the selected files, which the local xeda publishes into a mirror."""
