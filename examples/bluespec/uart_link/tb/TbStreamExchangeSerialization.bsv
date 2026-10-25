// Output serialization of the byte-aligned (fast) adapter for several output widths.
package TbStreamExchangeSerialization;

import FIFOF :: *;
import GetPut :: *;
import LeanLink :: *;
import StmtFSM :: *;
import StreamExchange :: *;
import Vector :: *;

interface SerializationLink;
   interface LeanLink#(0, 8) link;
   method Action write(Bit#(8) word);   // request: DATA opcode, one input word
   method Action poll;                  // request: DATA opcode only
   method Action setResponseMax(UInt#(8) n);
   interface Get#(LinkTok) hostResponse;
endinterface

// A packet-level link double. After every response token it holds `responseReady` low for
// three cycles, so each output byte is produced under backpressure.
module mkSerializationLink(SerializationLink);
   FIFOF#(Bit#(8)) unusedRx <- mkFIFOF;
   FIFOF#(Bit#(8)) unusedTx <- mkFIFOF;
   FIFOF#(LinkTok) responses <- mkSizedFIFOF(16);
   Reg#(UInt#(8)) maxResponse <- mkReg(2);
   Reg#(UInt#(8)) payload <- mkReg(0);
   Reg#(UInt#(8)) remaining <- mkReg(0);
   Reg#(Bit#(8)) word <- mkReg(0);
   Reg#(UInt#(2)) gap <- mkReg(0);
   PulseWire tokenPut <- mkPulseWire;

   (* fire_when_enabled, no_implicit_conditions *)
   rule updateGap;
      if (tokenPut) gap <= 3;
      else if (gap != 0) gap <= gap - 1;
   endrule

   interface LeanLink link;
      interface rx = toPut(unusedRx);
      interface tx = toGet(unusedTx);
      interface Get request;
         method ActionValue#(LinkTok) get if (remaining != 0);
            remaining <= remaining - 1;
            if (remaining == payload)
               return LinkTok { data: 8'h01, last: payload == 1, empty: False };
            else
               return LinkTok { data: word, last: True, empty: False };
         endmethod
      endinterface
      interface Put response;
         method Action put(LinkTok item) if (gap == 0);
            responses.enq(item);
            tokenPut.send;
         endmethod
      endinterface
      method Bool responseReady = gap == 0;
      method UInt#(8) responseMax = maxResponse;
      method UInt#(8) requestPayloadLength = payload;
   endinterface

   method Action write(Bit#(8) w) if (remaining == 0);
      word <= w;
      payload <= 2;
      remaining <= 2;
   endmethod
   method Action poll if (remaining == 0);
      payload <= 1;
      remaining <= 1;
   endmethod
   method Action setResponseMax(UInt#(8) n);
      maxResponse <= n;
   endmethod
   interface hostResponse = toGet(responses);
endmodule

interface SerializationCase#(numeric type outW);
   method Action start;
   method Bool done;
endinterface

// Two exchanges, each with one input word, move two known output words through the adapter. The
// second word is produced while the first one is serialized. Expected bytes come from integer
// division, not from bit selects. The most significant bit of the first word is set, so
// a sign-extended or unmasked final byte differs from the zero-padded expectation.
module mkSerializationCase(SerializationCase#(outW))
   provisos (Add#(outPad, 1, outW));
   SerializationLink host <- mkSerializationLink;
   StreamExchange#(Bit#(8), Bit#(outW)) stream <- mkStreamExchange(host.link);

   Integer outBytes = (valueOf(outW) + 7) / 8;
   Integer msb = 2 ** (valueOf(outW) - 1);
   Integer pattern0 = 'h0123456789ABCDEFFEDCBA9876543210;
   Integer pattern1 = 'hF0E1D2C3B4A5968778695A4B3C2D1E0F;
   Integer word0 = (pattern0 % msb) + msb;
   Integer word1 = ((2 ** 128 - 1 - pattern1) % msb);

   function Bit#(8) byteOf(Integer word, Integer i) = fromInteger((word / (2 ** (8 * i))) % 256);
   Vector#(TDiv#(outW, 8), Bit#(8)) bytes0 = genWith(byteOf(word0));
   Vector#(TDiv#(outW, 8), Bit#(8)) bytes1 = genWith(byteOf(word1));

   Reg#(UInt#(2)) producedState <- mkReg(0, reset_by stream.coreReset);
   Wire#(UInt#(2)) produced <- mkDWire(0, reset_by noReset);
   rule observeProduced; produced <= producedState; endrule

   rule core;
      let inputWord <- stream.inputWords.get;
      Bit#(8) expectedInput = producedState == 0 ? 8'hA5 : 8'h5A;
      if (inputWord != expectedInput)
         $fatal(1, "FAIL: width %0d input word %0d was %h", valueOf(outW), producedState, inputWord);
      stream.outputWords.put(fromInteger(producedState == 0 ? word0 : word1));
      producedState <= producedState + 1;
   endrule

   Reg#(UInt#(9)) index <- mkReg(0);
   Reg#(Bool) passed <- mkReg(False);

   function Action expectToken(String what, Bit#(8) data, Bool last) = action
      let token <- host.hostResponse.get;
      if (token.data != data || token.last != last || token.empty)
         $fatal(1, "FAIL: width %0d %s: got data=%h last=%b empty=%b, expected data=%h last=%b",
                valueOf(outW), what, token.data, token.last, token.empty, data, last);
   endaction;

   // A response without output is status 0 and the accepted-word count.
   function Stmt expectNoOutput(Bit#(8) accepted) = seq
      expectToken("status", 0, False);
      expectToken("count", accepted, True);
   endseq;

   // Status 0 and count 0 are followed by the output bytes, least significant first.
   function Stmt expectOutput(Vector#(TDiv#(outW, 8), Bit#(8)) bytes) = seq
      expectToken("status", 0, False);
      expectToken("count", 0, False);
      index <= 0;
      while (index < fromInteger(outBytes)) action
         expectToken("output byte", bytes[index], index + 1 == fromInteger(outBytes));
         index <= index + 1;
      endaction
   endseq;

   FSM fsm <- mkFSM(seq
      host.write(8'hA5);
      expectNoOutput(1);
      while (produced != 1) action noAction; endaction
      // The core cannot produce the second word before the first one leaves the output queue.
      host.write(8'h5A);
      expectNoOutput(1);
      host.setResponseMax(fromInteger(outBytes + 2));
      host.poll;
      expectOutput(bytes0);
      while (produced != 2) action noAction; endaction
      host.poll;
      expectOutput(bytes1);
      host.poll;
      expectNoOutput(0);
      passed <= True;
   endseq);

   method Action start = fsm.start;
   method Bool done = passed;
endmodule

(* synthesize *)
module mkTbStreamExchangeSerialization(Empty);
   SerializationCase#(1) width1 <- mkSerializationCase;
   SerializationCase#(7) width7 <- mkSerializationCase;
   SerializationCase#(9) width9 <- mkSerializationCase;
   SerializationCase#(13) width13 <- mkSerializationCase;
   SerializationCase#(20) width20 <- mkSerializationCase;
   SerializationCase#(25) width25 <- mkSerializationCase;
   SerializationCase#(57) width57 <- mkSerializationCase;
   SerializationCase#(64) width64 <- mkSerializationCase;
   SerializationCase#(1024) width1024 <- mkSerializationCase;
   Reg#(UInt#(32)) cycles <- mkReg(0);

   rule watchdog;
      cycles <= cycles + 1;
      if (cycles == 20000) $fatal(1, "FAIL: serialization test timed out");
   endrule

   Stmt script = seq
      action
         width1.start; width7.start; width9.start; width13.start;
         width20.start; width25.start; width57.start; width64.start;
         width1024.start;
      endaction
      while (!(width1.done && width7.done && width9.done && width13.done && width20.done
               && width25.done && width57.done && width64.done && width1024.done))
         action noAction; endaction
      action
         $display("PASS: output widths 1, 7, 9, 13, 20, 25, 57, 64 and 1024 serialize zero-padded little-endian bytes");
         $finish(0);
      endaction
   endseq;

   mkAutoFSM(script);
endmodule

endpackage
