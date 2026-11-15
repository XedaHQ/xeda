- A sweep worker refuses a node from a process that runs other Xeda code, such as a notebook kernel
  that ran while Xeda was updated: the node fails with `stale_runtime`, which says to restart that
  process. Before, every node failed with `internal_error`.
