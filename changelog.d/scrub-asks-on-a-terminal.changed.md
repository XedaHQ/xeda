- `xeda scrub` now asks on a terminal only, as `xeda run --prune-variants` does. Without a terminal
  it removes nothing, so `echo yes | xeda scrub` no longer removes: pass `--yes`. Both commands now
  report `N run directories removed.`
