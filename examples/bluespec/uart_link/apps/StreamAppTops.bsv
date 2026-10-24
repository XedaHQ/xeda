// Application tops for byte-interface and complete UART simulation.
package StreamAppTops;

`ifndef LL_LOGW
`define LL_LOGW 3
`endif
`ifndef LL_LOGP
`define LL_LOGP 8
`endif

import GetPut :: *;
import LeanLink :: *;
import LeanLinkBoard :: *;
`ifdef LL_APP_ASCON
import StreamExchange :: *;
import AsconStream :: *;
`elsif LL_APP_GCD
import StreamExchange :: *;
import GcdStream :: *;
`else
import LeanLinkTestTop :: *;
`endif

interface StreamAppBytes;
   interface Put#(Bit#(8)) rx;
   interface Get#(Bit#(8)) tx;
endinterface

(* synthesize *)
module mkStreamAppBytes(StreamAppBytes);
   LeanLink#(`LL_LOGW, `LL_LOGP) link <- mkLeanLink;
`ifdef LL_APP_ASCON
   StreamExchange#(Bit#(40), Bit#(40)) stream <- mkStreamExchange(link);
   Empty application <- mkAsconStream(stream);
`elsif LL_APP_GCD
   StreamExchange#(Bit#(64), Bit#(32)) stream <- mkStreamExchange(link);
   Empty application <- mkGcdStream(stream);
`else
   LeanLinkTestTop demo <- mkLeanLinkTestTopOf(link);
   (* fire_when_enabled, no_implicit_conditions *)
   rule noStall;
      demo.stall(False);
   endrule
`endif
   interface rx = link.rx;
   interface tx = link.tx;
endmodule

// Expose the reset in simulation. Board builds use mkLeanLinkBoard and its power-on reset.
(* synthesize *)
module mkStreamAppUart(LeanLinkBoard);
   let pins <- mkLinkDemo;
   return pins;
endmodule

endpackage
