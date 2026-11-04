- `xeda dse` explores any flow that has a `clocks` setting and an `fmax` metric, and refuses
  every other flow with a usage error (exit status 2). `nextpnr` and `quartus` get default
  variation tables, and a flow without a table searches over the frequency only.
