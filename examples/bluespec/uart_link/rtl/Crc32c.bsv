// Reflected CRC-32C with a selectable number of update bits per clock.
package Crc32c;

function Bit#(32) crc32cBit(Bit#(32) value);
`ifdef XEDA_INJECT_BUG
   return (value >> 1) ^ ((value[0] == 1) ? 32'hedb88320 : 0);
`else
   return (value >> 1) ^ ((value[0] == 1) ? 32'h82f63b78 : 0);
`endif
endfunction

function Bit#(32) crc32cUpdate(Bit#(32) state, Bit#(8) value);
   Bit#(32) next = state ^ zeroExtend(value);
   for (Integer bitIndex = 0; bitIndex < 8; bitIndex = bitIndex + 1)
      next = crc32cBit(next);
   return next;
endfunction

interface Crc32c;
   method Action clear;
   method Action put(Bit#(8) value);
   method Bool ready;
   method Bit#(32) state;
endinterface

// clear and put are available only when ready. state has no final complement;
// after the message and its little-endian CRC, the valid residue is B798B438.
module mkCrc32c#(Integer bitsPerCycle)(Crc32c);
   if (bitsPerCycle != 1 && bitsPerCycle != 2 && bitsPerCycle != 4 && bitsPerCycle != 8)
      error("mkCrc32c: bitsPerCycle must be 1, 2, 4, or 8");
   Reg#(Bit#(32)) current <- mkReg('1);
   Reg#(UInt#(4)) remaining <- mkReg(0);

   rule update (remaining != 0);
      Bit#(32) next = current;
      for (Integer bitIndex = 0; bitIndex < bitsPerCycle; bitIndex = bitIndex + 1)
         next = crc32cBit(next);
      current <= next;
      remaining <= remaining - fromInteger(bitsPerCycle);
   endrule

   method Action clear if (remaining == 0);
      current <= '1;
   endmethod

   method Action put(Bit#(8) value) if (remaining == 0);
      current <= current ^ zeroExtend(value);
      remaining <= 8;
   endmethod

   method Bool ready = remaining == 0;
   method Bit#(32) state = current;
endmodule

endpackage
