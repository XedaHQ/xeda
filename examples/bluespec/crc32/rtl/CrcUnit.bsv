// CrcUnit (BSV): a streaming CRC-32 unit built around the BH package CrcCore.
//
// Messages arrive as a stream of 32-bit chunks, each carrying 1 to 4 valid bytes (least
// significant byte first) and a `last` flag. The unit folds one chunk per cycle into the CRC
// with CrcCore's `mkCrcEngine` (a BH module synthesized separately) and answers each message
// with its CRC-32.
package CrcUnit;

import FIFO :: *;
import GetPut :: *;
import ClientServer :: *;
import CrcCore :: *;  // a BH (.bs) package

export Crc;
export CrcChunk(..);
export CrcUnit;
export mkCrcUnit;

typedef struct {
   Bit#(32) data;
   UInt#(3) nbytes;  // 1 to 4
   Bool last;
} CrcChunk deriving (Bits, Eq, FShow);

typedef Server#(CrcChunk, Crc) CrcUnit;

(* synthesize *)
module mkCrcUnit(CrcUnit);
   FIFO#(CrcChunk) inQ <- mkFIFO;
   FIFO#(Crc) outQ <- mkFIFO;
   CrcEngine engine <- mkCrcEngine;

   Reg#(Bool) first <- mkReg(True);     // the next chunk starts a message
   Reg#(Bool) finished <- mkReg(False); // the engine holds a complete message's CRC

   rule consume (!finished);
      let c = inQ.first;
      inQ.deq;
`ifdef XEDA_INJECT_BUG
      engine.update(c.data, 4, first); // deliberate bug for negative tests: ignores nbytes
`else
      engine.update(c.data, c.nbytes, first);
`endif
      first <= c.last;
      finished <= c.last;
   endrule

   rule report (finished);
      outQ.enq(engine.crc);
      finished <= False;
   endrule

   interface request = toPut(inQ);
   interface response = toGet(outQ);
endmodule

endpackage
