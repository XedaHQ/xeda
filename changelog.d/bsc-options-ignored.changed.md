- The `bsc` and `bsc_sim` flows refuse `BSC_OPTIONS` in the `env` setting. bsc runs without the
  variable, and a change of the caller's value still runs the flow again. Put bsc options in
  `extra_flags`.
