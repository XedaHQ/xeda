- The `bsc` and `bsc_sim` flows refuse `BSC_OPTIONS` in the `env` setting. They run bsc with an
  empty `BSC_OPTIONS` and warn when the caller's value is set. Put bsc options in `extra_flags`.
