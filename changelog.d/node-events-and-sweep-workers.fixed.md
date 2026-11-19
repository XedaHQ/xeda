- Every node of a result now has its `node_started` and `node_finished` events, except a `skipped`
  or `detached` node, in a launch, a sweep and a remote launch alike. A sweep starts all its worker
  processes before its first node, so a failure to start one runs no node.
