- The documentation and the agent skill warn that openXC7 `nextpnr` builds from before
  2026-10-07 can write wrong bits with no error, and list the known defects. They also say
  that `nextpnr` ignores `set_property PULLUP true`: use `PULLTYPE PULLUP`.
