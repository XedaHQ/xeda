// Clock gate for the sky130hd platform, written for xeda.
//
// A design that gates a clock instantiates OPENROAD_CLKGATE. The module has the ports CK (the
// clock), E (the enable) and GCK (the gated clock), the same as the clock gate module that
// OpenROAD-flow-scripts defines, so a design written for that flow maps in the same way.
//
// With the macro OPENROAD_CLKGATE defined, the module is the integrated clock gate
// sky130_fd_sc_hd__dlclkp_1, which has the ports CLK, GATE and GCLK. Without the macro, the
// module passes the clock through and gates nothing.
module OPENROAD_CLKGATE (CK, E, GCK);
  input CK;
  input E;
  output GCK;

`ifdef OPENROAD_CLKGATE

  sky130_fd_sc_hd__dlclkp_1 latch (.CLK(CK), .GATE(E), .GCLK(GCK));

`else

  assign GCK = CK;

`endif

endmodule
