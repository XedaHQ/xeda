- A run whose checks reject its results (a failed test, negative slack in a report) is no longer
  run again on an unchanged launch: Xeda reports the recorded failure (`--rebuild-all` runs it).
  In `xeda run --json`, such a node is `rejected again`, and a node whose producer failed is
  `blocked`.
