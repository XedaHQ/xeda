- `cocotb.testcase` and `cocotb.gpi_extra` given as text now follow the rule of every list setting:
  empty items are dropped, and `[]` or `[a,b]` is an error. `-s cocotb.testcase=[]` used to select
  a test named `[]`.
