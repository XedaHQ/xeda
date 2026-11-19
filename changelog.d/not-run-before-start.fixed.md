- A node that fails or is canceled before its run begins, such as a sweep task that a worker
  refuses with `stale_runtime`, now has the disposition `not_run` instead of `ran`. `on_event` now
  gets the events of every node that failed, and a remote launch tells the node events that the
  launch on the host told.
