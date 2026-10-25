- `xeda run --remote` now sends explicit input bindings (`flows.<flow>.inputs.<input>`) to the host.
  Before, the host used the default producer, and the run failed with `RemoteIncompatible`.
