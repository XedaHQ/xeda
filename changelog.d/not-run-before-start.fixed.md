- A node that fails or is canceled before its run begins, such as a sweep task that a worker
  refuses with `stale_runtime`, now has the disposition `not_run` instead of `ran`.
