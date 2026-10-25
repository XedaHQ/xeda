- `xeda run --remote` no longer fails on an artifact that is a symbolic link. A link to a file in
  the run directory brings that file back, and `--outputs-to` skips a link that leads out of it.
