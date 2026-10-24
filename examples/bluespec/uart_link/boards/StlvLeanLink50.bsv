// STLV7325 v2 wrapper with a physical 200 MHz input and a generated 50 MHz core clock.
package StlvLeanLink50;

import LeanLinkBoard :: *;

interface Clock50;
   interface Clock clk;
endinterface

import "BVI" StlvClock50 =
module vStlvClock50#(Clock p, Bit#(1) n)(Clock50);
   default_clock no_clock;
   default_reset no_reset;
   input_clock pin(CLK_P, (* unused *) CLK_P_GATE) = p;
   port CLK_N clocked_by(pin) = n;
   output_clock clk(CLK_50M);
   same_family(pin, clk);
endmodule

interface StlvLeanLink;
   (* always_ready, result = "uart_tx" *)
   method Bit#(1) uart_tx;
   (* always_ready, result = "led" *)
   method Bit#(8) led;
endinterface

(* synthesize, default_clock_osc = "clk_p", default_gate_inhigh, no_default_reset *)
module mkStlvLeanLink50#(Bit#(1) clk_n, Bit#(1) uart_rx, Bit#(2) btn)(StlvLeanLink);
   Clock pad <- exposeCurrentClock;
   Clock50 clocks <- vStlvClock50(pad, clk_n);
   LeanLinkBoard board <- mkLeanLinkBoard(clocked_by clocks.clk, reset_by noReset);

   (* fire_when_enabled, no_implicit_conditions *)
   rule feed;
      board.inputs(uart_rx, 0, ~btn[1]);
   endrule

   method Bit#(1) uart_tx = board.txd;
   method Bit#(8) led = {4'hF, ~board.led};
endmodule

endpackage
