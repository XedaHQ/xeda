- `xeda dse` runs each round as one sweep, and its `--json` document has `outcome`, `error`,
  `best`, `best_run_dir` and `points`, with every failed point kept. A failure of the optimizer
  ends the command with an error and keeps what the exploration found, and a usage error exits with
  status 2.
