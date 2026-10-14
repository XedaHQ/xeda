- A run that completes but fails its checks (timing with `fail_timing`, a failed test) is no
  longer run again on an unchanged launch: Xeda reports the recorded failure. Use `--rebuild-all`
  to run it again.
