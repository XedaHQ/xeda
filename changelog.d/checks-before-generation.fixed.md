- A launch now refuses what the design's declarations already rule out (a source the flow cannot
  read, a wrong setting, a run directory the flow cannot use) before the design's generator runs.
  A file that a setting reads, such as `custom_boards_file`, must now exist before the generator
  runs.
