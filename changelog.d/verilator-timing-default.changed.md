- Verilator now enables timing by default, so testbench delays and event controls run as written.
  Set `timing: false` to disable timing; Verilator then warns when it ignores a delay. Timing
  support requires a compiler with C++20 coroutine support.
