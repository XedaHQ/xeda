// Synthesizable profiles of LeanLink for area measurement. Every port is observable.
package LeanLinkTops;

import LeanLink :: *;

// Pipelined profile: W = 8 pages of 256 bytes per direction.
(* synthesize *)
module mkLeanLinkW8P256(LeanLink#(3, 8));
   let m <- mkLeanLink;
   return m;
endmodule

// Minimal profile: W = 1 page of 64 bytes per direction.
(* synthesize *)
module mkLeanLinkW1P64(LeanLink#(0, 6));
   let m <- mkLeanLink;
   return m;
endmodule

// Larger window: W = 32 pages of 256 bytes.
(* synthesize *)
module mkLeanLinkW32P256(LeanLink#(5, 8));
   let m <- mkLeanLink;
   return m;
endmodule

endpackage
