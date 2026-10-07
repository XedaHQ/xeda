- The documentation and the agent skill warn that openXC7 `nextpnr` builds before commit
  `18362b3` can write wrong bits with no error. They also say that `nextpnr` ignores
  `set_property PULLUP true`: use `PULLTYPE PULLUP`.
