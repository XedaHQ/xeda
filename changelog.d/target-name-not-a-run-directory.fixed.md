- A target can no longer be named like a flow's hashed run directory (`<flow>_` and a 16-character
  hash). `xeda scrub` took such a directory, emptied by `--clean`, for a target's and listed the
  run directories inside it. A design file with such a target name is now refused.
