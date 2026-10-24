// Application tops for byte-interface and complete UART simulation.
package StreamAppTops;

import GetPut :: *;
import LeanLink :: *;
import LeanLinkBoard :: *;
import StreamExchange :: *;
`ifdef LL_APP_ASCON
import AsconStream :: *;
`else
import GcdStream :: *;
`endif

interface StreamAppBytes;
   interface Put#(Bit#(8)) rx;
   interface Get#(Bit#(8)) tx;
endinterface

(* synthesize *)
module mkStreamAppBytes(StreamAppBytes);
   LeanLink#(3, 8) link <- mkLeanLink;
`ifdef LL_APP_ASCON
   StreamExchange#(Bit#(40), Bit#(40)) stream <- mkStreamExchange(link);
   Empty application <- mkAsconStream(stream);
`else
   StreamExchange#(Bit#(64), Bit#(32)) stream <- mkStreamExchange(link);
   Empty application <- mkGcdStream(stream);
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
