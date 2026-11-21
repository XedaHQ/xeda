- The `bsc` and `bsc_sim` flows refuse path flags such as `-p`, `-vsearch` and `-bdir` in
  `extra_flags`. Use `search_paths`, `installed_libraries`, `verilog_search_paths`, `bobj_dir`,
  `info_dir` or `verilog_out_dir`/`sim_dir` instead. Each listed file needs a unique name, also
  when letter case is ignored.
