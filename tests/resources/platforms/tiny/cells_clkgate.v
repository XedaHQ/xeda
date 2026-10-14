// written for xeda's tests
// A clock gate from an AND gate.
module TINY_CLKGATE (input CK, input E, output GCK);
  assign GCK = CK & E;
endmodule
