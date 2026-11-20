- The `bsc` and `bsc_sim` flows read a package, an include file or a Verilog module of the design
  only from a listed source or from a folder in `search_paths`. A design that imports an unlisted
  package or includes an unlisted file now fails: list the file, or name its folder in
  `search_paths`. A folder in `search_paths` hides a stray `.bo` file beside its source, and any
  file that defines a package that the design lists. A missing `search_paths` or
  `verilog_search_paths` folder now fails the run.
