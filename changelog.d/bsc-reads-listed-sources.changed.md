- The `bsc` and `bsc_sim` flows read a package, an include file or a Verilog module of the design
  only from a listed source or from a folder in `search_paths`. A design that imports an unlisted
  package or includes an unlisted file now fails: list the file, or name its folder in
  `search_paths`. A folder in `search_paths` hides a stray `.bo` file beside its source, and any
  file that defines a package that the design lists. A missing `search_paths` or
  `verilog_search_paths` folder now fails the run.
  The flows now refuse, before they run, two different listed files with one file name, a `.bsv`
  and a `.bs` file of one package, and a Bluespec source with another suffix. They refuse a path
  flag (`-p`, `-vsearch`, `-bdir`, `-info-dir`, `-fdir`, `-vdir`, `-simdir`, `-i`, and `-I` and
  `-L` in `bsc_sim`) in `extra_flags`: use the setting of that path. They refuse an output folder
  that is the run directory or lies inside a stage folder. bsc now runs with an empty
  `BSC_OPTIONS`, so it ignores that variable of the caller and the flow warns about it. The `env`
  setting must not set it.
  bsc's messages in the console and in Xeda's summary name the real files. The log keeps bsc's
  text (`bsc_sources/...`).
