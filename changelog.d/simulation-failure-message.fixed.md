- A simulation that ends with no finish (for example a testbench that stops at `wait;`) now says
  so in `error.message` of `results.json`, and tells you to call `std.env.finish` or `$finish`, or
  to set `stop_time`. A failed cocotb run also reports its test counts there.
