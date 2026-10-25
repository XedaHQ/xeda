- `--outputs-to` no longer copies what a symbolic link left by a tool leads to outside the run
  directory: it skips that artifact with a warning. A location that a setting names for it is
  refused.
