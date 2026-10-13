- The `openroad` settings `input_delay` and `output_delay`, which are in picoseconds, now reach the
  constraints in the platform's time unit. On a platform in nanoseconds, such as `nangate45`,
  `input_delay: 100` was written as 100 ns. `clocks.sdc` also writes the clock period and the
  delays with six decimals, so a delay of less than a picosecond is kept.
