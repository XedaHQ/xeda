- In a `search_paths` folder, bsc no longer reads a stray compiled `.bo` beside its source, or a
  file that defines a package the design lists. A missing `search_paths` or
  `verilog_search_paths` folder now fails the run.
