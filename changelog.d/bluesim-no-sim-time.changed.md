- `bsc_sim` with Bluesim no longer reports `sim.time`, because the Bluesim kernel counts unitless
  ticks. `sim.evidence` keeps the cycle count, and its times are ticks with a null `time_unit`.
