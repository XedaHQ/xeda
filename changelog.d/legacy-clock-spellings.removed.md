- The design key `clock_port` and the flow setting `clock_period` were removed, and each now fails
  with its replacement. Write `rtl.clocks: [{port: <port>}]` in the design and
  `clocks.<name>.period` (or `clocks.<name>.freq`) in the flow settings.
