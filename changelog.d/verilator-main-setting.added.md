- `verilator` has a `main` setting for `--main`. Set `main: false` when the design's C++ sources
  hold `main` or `sc_main`; a C++ source with `main` unset is a helper (DPI) and now links.
