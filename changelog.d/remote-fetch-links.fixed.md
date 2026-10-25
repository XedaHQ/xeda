- `xeda run --remote` now treats a symbolic link that a tool left as a local launch does: an
  artifact that a link leads to a file of the run directory comes back, and `--outputs-to` skips
  one that a link leads out of it, where the fetch failed before.
