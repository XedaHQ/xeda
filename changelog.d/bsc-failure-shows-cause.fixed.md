- A failed Bluespec compile or link now reports the first errors of the C++ compiler, `make` and
  bsc in the error message, and keeps the output in `bsc_compile.log` or `bsc_link.log`. Before,
  it said only that bsc exited with a nonzero code.
