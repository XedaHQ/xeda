- `Workspace.run` takes a `CancelToken` that stops a launch from another thread or after a time,
  and an `on_event` callback that reports the start and end of the launch and of each node. A
  canceled launch returns the outcome `canceled`.
