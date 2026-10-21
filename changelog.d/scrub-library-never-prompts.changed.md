- `scrub_design` and a launch with `scrub_old_runs` no longer prompt on the terminal. They remove
  nothing unless the caller passes a confirmation (`confirm`, `confirm_scrub`). `xeda scrub` and
  `xeda run --scrub` still ask, and `xeda run --remote --scrub` now keeps the old runs and logs it.
