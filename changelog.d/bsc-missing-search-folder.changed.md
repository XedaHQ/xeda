- A missing `search_paths` or `installed_libraries` folder now fails the `bsc` and `bsc_sim` run,
  whatever the warning settings. A missing `verilog_search_paths` folder fails a run with a Verilog
  backend. Fix or remove the entry.
