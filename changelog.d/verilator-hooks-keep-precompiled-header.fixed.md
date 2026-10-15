- A Verilator model large enough for Verilator to compile in parallel now builds with GCC in the
  `verilator` and `bsc_sim` flows. Before, the build failed on a missing `__pch.h.slow` header.
