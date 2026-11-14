- A script that starts a launch before `if __name__ == "__main__":` now fails in each sweep worker
  with `unguarded_main`, and the error of every point names the missing guard. It no longer runs
  the launch again in each worker, or ends with "process pool was terminated abruptly".
