// Synthesizable tops of the link with the test endpoint, for the cocotb tests: one per profile.
package LeanLinkCosimTops;

import LeanLink :: *;
import LeanLinkTestTop :: *;

// Pipelined profile: W = 8 pages of 256 bytes.
(* synthesize *)
module mkLeanLinkCosimW8(LeanLinkTestTop);
   LeanLink#(3, 8) link <- mkLeanLink;
   let m <- mkLeanLinkTestTopOf(link);
   return m;
endmodule

// Minimal profile: W = 1 page of 64 bytes.
(* synthesize *)
module mkLeanLinkCosimW1(LeanLinkTestTop);
   LeanLink#(0, 6) link <- mkLeanLink;
   let m <- mkLeanLinkTestTopOf(link);
   return m;
endmodule

endpackage
