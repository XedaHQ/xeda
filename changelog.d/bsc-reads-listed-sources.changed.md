- The `bsc` and `bsc_sim` flows find a package, include file or Verilog module of the design only
  in a listed source or a `search_paths` folder. A design that imports or includes an unlisted
  file now fails: list the file, or name its folder in `search_paths`.
