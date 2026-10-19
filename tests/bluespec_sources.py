"""Bluespec sources that tests write into a design."""

#: A counter with one synthesized module, `mkCounter`, and a comment line to edit.
COUNTER_BSV = """package Counter;

// A counter.
(* synthesize *)
module mkCounter(Reg#(UInt#(8)));
   Reg#(UInt#(8)) count <- mkReg(0);
   rule tick;
      count <= count + 1;
   endrule
   return count;
endmodule

endpackage
"""
