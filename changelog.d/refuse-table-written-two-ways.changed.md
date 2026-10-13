- A mapping that writes one table both dotted and as a block (`fpga.part: P` beside
  `fpga: {speed: -6}`) is now an error that names both spellings. It used to keep the keys of
  whichever came last. Write the table in one spelling.
