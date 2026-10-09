- The yosys flows no longer apply `chparam` to a VHDL top when a constraint file or another source
  that is not HDL follows it in the design's sources: yosys failed on such a design.
