- The `bsc` and `bsc_sim` flows find a package, an include file or a Verilog module of the design
  only in a listed source or in a folder of the settings. A design that uses an unlisted file now
  fails: list the file, or name its folder in `search_paths` (`verilog_search_paths` for a Verilog
  module).
