- Remove the `remote` parameter of `xeda.sweep`: a sweep never ran on a remote host, so the
  parameter only raised. Use `xeda.run` with a `Remote` to launch on a host.
