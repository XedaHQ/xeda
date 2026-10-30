- Add the `Remote` value. `Workspace(remote=Remote(host, profile=...))` plans and runs a spec on a
  host and returns a `Plan` or a `Result`. A host that cannot be reached or set up now gives the
  codes `remote_unreachable` and `remote_setup_failed`.
