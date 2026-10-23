- The `error` of a node in `results.json` has a stable `code` (`nonzero_exit`, `timeout`,
  `reported_failure`, `producer_failed` and so on) instead of `type`, and a failed producer is
  quoted with its code. Read `error.code` where you read `error.type`.
