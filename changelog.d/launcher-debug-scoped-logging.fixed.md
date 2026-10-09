- A launcher's `debug` shows xeda's own DEBUG records for the launch only, and no longer sets the
  root logger of the process to DEBUG for good. A design-space search removes its log file handler
  when it ends.
