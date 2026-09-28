// Self-checking testbench for mkCrcUnit.
//
// Sends two known-answer messages ("123456789", whose CRC-32 is the standard check value
// CBF43926, and "The quick brown fox jumps over the lazy dog") and pseudo-random messages of
// 1 to 32 bytes. Each message's expected CRC comes from a table-driven reference (one table
// lookup per byte, unlike the bit-serial CrcCore), which is itself checked against the known
// answers.
package TbCrcUnit;

import FIFO :: *;
import GetPut :: *;
import ClientServer :: *;
import Vector :: *;
import LFSR :: *;
import StmtFSM :: *;
import CrcUnit :: *;

typedef 64 NumMessages;
UInt#(32) maxCycles = 5000;

typedef 48 MaxKatLength;
typedef Vector#(MaxKatLength, Bit#(8)) KatBytes;

// A string's characters as bytes, zero-padded.
function KatBytes stringBytes(String s);
   List#(Bit#(8)) chars = List::map(compose(fromInteger, charToInteger), stringToCharList(s));
   return toVector(List::append(chars, List::replicate(valueOf(MaxKatLength) - List::length(chars), 0)));
endfunction

String kat0 = "123456789";
String kat1 = "The quick brown fox jumps over the lazy dog";
Vector#(2, KatBytes) katBytes = cons(stringBytes(kat0), cons(stringBytes(kat1), nil));
Vector#(2, UInt#(8)) katLengths = cons(fromInteger(stringLength(kat0)),
                                       cons(fromInteger(stringLength(kat1)), nil));
Vector#(2, Crc) katCrcs = cons('hCBF43926, cons('h414FA339, nil));

// Table-driven reference: crc' = table[(crc ^ byte) & 0xFF] ^ (crc >> 8)
function Crc tableEntry(Integer i);
   Crc c = fromInteger(i);
   for (Integer k = 0; k < 8; k = k + 1)
      c = c[0] == 1 ? (c >> 1) ^ 'hEDB88320 : c >> 1;
   return c;
endfunction

Vector#(256, Crc) crcTable = genWith(tableEntry);

function Crc refUpdate(Crc c, Bit#(32) data, UInt#(3) nbytes);
   for (Integer i = 0; i < 4; i = i + 1)
      if (fromInteger(i) < nbytes)
         c = crcTable[c[7:0] ^ data[8 * i + 7 : 8 * i]] ^ (c >> 8);
   return c;
endfunction

(* synthesize *)
module mkTbCrcUnit(Empty);
   CrcUnit dut <- mkCrcUnit;

   LFSR#(Bit#(32)) lfsr <- mkLFSR_32;
   FIFO#(Crc) expected <- mkSizedFIFO(4);
   Reg#(UInt#(8)) msg <- mkReg(0);
   Reg#(UInt#(8)) len <- mkReg(0);
   Reg#(UInt#(8)) pos <- mkReg(0);
   Reg#(Crc) refCrc <- mkReg(0);
   Reg#(UInt#(8)) nChecked <- mkReg(0);
   Reg#(UInt#(32)) cycle <- mkReg(0);

   rule watchdog;
      cycle <= cycle + 1;
      if (cycle == maxCycles)
         $fatal(1, "FAIL: timeout after %0d cycles", cycle);
   endrule

   // The next up-to-four bytes of message `msg` from position `pos`.
   function Bit#(32) chunkData();
      if (msg < 2) begin
         KatBytes b = katBytes[msg];
         return {b[pos + 3], b[pos + 2], b[pos + 1], b[pos]};
      end
      else return lfsr.value;
   endfunction

   Stmt sender = seq
      for (msg <= 0; msg < fromInteger(valueOf(NumMessages)); msg <= msg + 1) seq
         action
            len <= msg < 2 ? katLengths[msg] : unpack(zeroExtend(lfsr.value[4:0])) + 1;
            pos <= 0;
            refCrc <= 'hFFFFFFFF;
            lfsr.next;
         endaction
         while (pos < len) action
            UInt#(8) left = len - pos;
            UInt#(3) n = left > 4 ? 4 : truncate(left);
            let data = chunkData;
            dut.request.put(CrcChunk { data: data, nbytes: n, last: left <= 4 });
            refCrc <= refUpdate(refCrc, data, n);
            pos <= pos + 4;
            lfsr.next;
         endaction
         action
            Crc crc = ~refCrc;
            if (msg < 2 && crc != katCrcs[msg])
               $fatal(1, "FAIL: reference model gives %h for known-answer message %0d", crc, msg);
            expected.enq(crc);
         endaction
      endseq
   endseq;

   // Not mkAutoFSM: that calls $finish(0) as soon as the sender is done, before the last
   // responses have been checked.
   FSM senderFsm <- mkFSM(sender);
   Reg#(Bool) started <- mkReg(False);

   rule start (!started);
      senderFsm.start;
      started <= True;
   endrule

   rule check;
      let crc <- dut.response.get;
      expected.deq;
      if (crc != expected.first)
         $fatal(1, "FAIL: message %0d: expected CRC %h, got %h", nChecked, expected.first, crc);
      nChecked <= nChecked + 1;
      if (nChecked + 1 == fromInteger(valueOf(NumMessages))) begin
         $display("PASS: CRC-32 of %0d messages checked in %0d cycles", nChecked + 1, cycle);
         $finish(0);
      end
   endrule
endmodule

endpackage
