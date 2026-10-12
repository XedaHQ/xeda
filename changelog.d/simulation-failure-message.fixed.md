- A simulation that ends with no finish (for example a testbench that stops at `wait;`) now says
  so in `error.message` of `results.json`, and tells you to call `std.env.finish` or `$finish`, or
  to set `stop_time`. Other failed endings and failed cocotb runs give their cause and a fix there
  too. A cocotb run that left no readable results file reports no test counts.
