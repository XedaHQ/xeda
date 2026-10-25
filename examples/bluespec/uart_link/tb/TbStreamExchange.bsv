// Directed tests for the packet-to-stream adapter and its GCD attachment.
package TbStreamExchange;

import ClientServer :: *;
import FIFOF :: *;
import GetPut :: *;
import GcdStream :: *;
import LeanLink :: *;
import StmtFSM :: *;
import StreamExchange :: *;

interface TestLink#(numeric type logP);
   interface LeanLink#(0, logP) link;
   interface Put#(LinkTok) hostRequest;
   interface Get#(LinkTok) hostResponse;
   method Action setResponseMax(UInt#(8) n);
   method UInt#(8) requestPayloadLength;
endinterface

// This packet-boundary double keeps the tests focused on the stream protocol. Serial framing,
// CRC and replay are tested by the LeanLink testbench and the application wire tests.
module mkTestLink(TestLink#(logP)) provisos (Add#(logP, _pad, 8));
   FIFOF#(LinkTok) requests <- mkSizedFIFOF(256);
   FIFOF#(LinkTok) responses <- mkSizedFIFOF(256);
   FIFOF#(Bit#(8)) unusedRx <- mkFIFOF;
   FIFOF#(Bit#(8)) unusedTx <- mkFIFOF;
   Reg#(UInt#(8)) maxResponse <- mkReg(8);
   Reg#(UInt#(8)) buildingLength <- mkReg(0);
   Reg#(UInt#(8)) requestLength <- mkReg(0);
   Reg#(Bool) requestComplete <- mkReg(False);

   interface LeanLink link;
      interface rx = toPut(unusedRx);
      interface tx = toGet(unusedTx);
      interface Get request;
         method ActionValue#(LinkTok) get if (requestComplete && requests.notEmpty);
            let item = requests.first;
            requests.deq;
            if (item.empty || item.last) begin
               requestComplete <= False;
               requestLength <= 0;
            end
            return item;
         endmethod
      endinterface
      interface response = toPut(responses);
      method Bool responseReady = True;
      method UInt#(8) responseMax = maxResponse;
      method UInt#(8) requestPayloadLength if (requestComplete) = requestLength;
   endinterface
   interface Put hostRequest;
      method Action put(LinkTok item) if (requests.notFull);
         requests.enq(item);
         if (item.empty) begin
            buildingLength <= 0;
            requestLength <= 0;
            requestComplete <= True;
         end else if (item.last) begin
            requestLength <= buildingLength + 1;
            buildingLength <= 0;
            requestComplete <= True;
         end else buildingLength <= buildingLength + 1;
      endmethod
   endinterface
   interface hostResponse = toGet(responses);
   method Action setResponseMax(UInt#(8) n); maxResponse <= n; endmethod
   method UInt#(8) requestPayloadLength if (requestComplete) = requestLength;
endmodule

function LinkTok byteTok(Bit#(8) b, Bool last) =
   LinkTok { data: b, last: last, empty: False };

(* synthesize *)
module mkTbStreamExchange(Empty);
   TestLink#(8) testLink <- mkTestLink;
   StreamExchange#(Bit#(12), Bit#(12)) stream <- mkStreamExchange(testLink.link);
   Reg#(Bool) pauseCore <- mkReg(True);
   Reg#(UInt#(8)) consumed <- mkReg(0, reset_by stream.coreReset);
   Reg#(UInt#(32)) cycles <- mkReg(0);
   Reg#(Bit#(8)) r0 <- mkReg(0);
   Reg#(Bit#(8)) r1 <- mkReg(0);
   Reg#(Bit#(8)) r2 <- mkReg(0);
   Reg#(Bit#(8)) r3 <- mkReg(0);
   Reg#(Bit#(8)) r4 <- mkReg(0);
   Reg#(Bool) last3 <- mkReg(False);
   Reg#(Bit#(8)) r5 <- mkReg(0);
   Reg#(Bit#(8)) r6 <- mkReg(0);
   Reg#(Bit#(8)) r7 <- mkReg(0);
   Reg#(Bool) last0 <- mkReg(False);
   Reg#(Bool) last1 <- mkReg(False);
   Reg#(Bool) last5 <- mkReg(False);
   Reg#(Bool) last7 <- mkReg(False);

   rule echoCore (!pauseCore);
      let word <- stream.inputWords.get;
      stream.outputWords.put(truncate(word + 1));
      consumed <= consumed + 1;
   endrule

   rule watchdog;
      cycles <= cycles + 1;
      if (cycles == 10000) $fatal(1, "FAIL: stream exchange test timed out");
   endrule

   Stmt script = seq
      // A fresh connection resets the application before it uses the stream.
      testLink.hostRequest.put(byteTok(8'h00, True));
      action let b <- testLink.hostResponse.get; r0 <= b.data; last0 <= b.last; endaction
      action let b <- testLink.hostResponse.get; r1 <= b.data; last1 <= b.last; endaction
      if (r0 != 0 || last0 || r1 != 0 || !last1 || consumed != 0)
         $fatal(1, "FAIL: initial RESET did not initialize the application");

      // Unsupported opcodes must reject the whole packet without admitting core input.
      testLink.hostRequest.put(byteTok(8'h02, True));
      action let b <- testLink.hostResponse.get; r0 <= b.data; last0 <= b.last; endaction
      action let b <- testLink.hostResponse.get; r1 <= b.data; last1 <= b.last; endaction
      if (r0 != 1 || last0 || r1 != 0 || !last1)
         $fatal(1, "FAIL: unsupported opcode response was not {1, 0}");
      if (consumed != 0) $fatal(1, "FAIL: unsupported opcode reached the core, consumed=%0d", consumed);

      // A complete packet is validated first, then admission stops at the one-word FIFO
      // boundary. The core is held so the accepted prefix is deterministic.
      testLink.hostRequest.put(byteTok(8'h01, False));
      testLink.hostRequest.put(byteTok(8'h01, False));
      testLink.hostRequest.put(byteTok(8'h00, False));
      testLink.hostRequest.put(byteTok(8'h23, False));
      testLink.hostRequest.put(byteTok(8'h01, False));
      testLink.hostRequest.put(byteTok(8'hbc, False));
      testLink.hostRequest.put(byteTok(8'h0a, False));
      testLink.hostRequest.put(byteTok(8'he6, False));
      testLink.hostRequest.put(byteTok(8'h05, False));
      testLink.hostRequest.put(byteTok(8'h89, False));
      testLink.hostRequest.put(byteTok(8'h07, False));
      testLink.hostRequest.put(byteTok(8'h34, False));
      testLink.hostRequest.put(byteTok(8'h02, True));
      action let b <- testLink.hostResponse.get; r0 <= b.data; last0 <= b.last; endaction
      action let b <- testLink.hostResponse.get; r1 <= b.data; last1 <= b.last; endaction
      if (r0 != 0 || last0 || r1 != 1 || !last1)
         $fatal(1, "FAIL: full input FIFO did not return accepted prefix 1");

      // The first accepted 12-bit word is returned intact in one poll.
      pauseCore <= False;
      while (consumed != 1) action noAction; endaction
      testLink.hostRequest.put(byteTok(8'h01, True));
      action let b <- testLink.hostResponse.get; r0 <= b.data; last0 <= b.last; endaction
      action let b <- testLink.hostResponse.get; r1 <= b.data; last1 <= b.last; endaction
      action let b <- testLink.hostResponse.get; r2 <= b.data; endaction
      action let b <- testLink.hostResponse.get; r3 <= b.data; last3 <= b.last; endaction
      if (r0 != 0 || last0 || r1 != 0 || last1
          || r2 != 8'h02 || r3 != 0 || !last3)
         $fatal(1, "FAIL: response split a word or serialized output incorrectly");

      // Incomplete words and nonzero high padding reject before any core admission.
      testLink.hostRequest.put(byteTok(8'h01, False));
      testLink.hostRequest.put(byteTok(8'h55, True));
      action let b <- testLink.hostResponse.get; r0 <= b.data; last0 <= b.last; endaction
      action let b <- testLink.hostResponse.get; r1 <= b.data; last1 <= b.last; endaction
      if (r0 != 1 || last0 || r1 != 0 || !last1)
         $fatal(1, "FAIL: incomplete input word was accepted");
      testLink.hostRequest.put(byteTok(8'h01, False));
      testLink.hostRequest.put(byteTok(8'h34, False));
      testLink.hostRequest.put(byteTok(8'hf2, True));
      action let b <- testLink.hostResponse.get; r0 <= b.data; last0 <= b.last; endaction
      action let b <- testLink.hostResponse.get; r1 <= b.data; last1 <= b.last; endaction
      if (r0 != 1 || last0 || r1 != 0 || !last1)
         $fatal(1, "FAIL: nonzero input padding was accepted");
      if (consumed != 1) $fatal(1, "FAIL: malformed packet reached the core");

      // RESET is a barrier: it resets the application state before success is visible.
      testLink.hostRequest.put(byteTok(8'h00, True));
      action let b <- testLink.hostResponse.get; r0 <= b.data; last0 <= b.last; endaction
      action let b <- testLink.hostResponse.get; r1 <= b.data; last1 <= b.last; endaction
      if (r0 != 0 || last0 || r1 != 0 || !last1 || consumed != 0)
         $fatal(1, "FAIL: RESET reply preceded the core reset");

      testLink.setResponseMax(1);
      testLink.hostRequest.put(byteTok(8'h01, False));
      testLink.hostRequest.put(byteTok(8'h11, False));
      testLink.hostRequest.put(byteTok(8'h00, True));
      action let b <- testLink.hostResponse.get;
         if (!b.empty || !b.last) $fatal(1, "FAIL: responseMax below two was not empty");
      endaction
      if (consumed != 0) $fatal(1, "FAIL: too-small response budget admitted input");

      // Leave one processed result queued, then verify RESET discards it before its reply.
      testLink.setResponseMax(8);
      pauseCore <= True;
      testLink.hostRequest.put(byteTok(8'h01, False));
      testLink.hostRequest.put(byteTok(8'h01, False));
      testLink.hostRequest.put(byteTok(8'h00, True));
      action let b <- testLink.hostResponse.get; r0 <= b.data; last0 <= b.last; endaction
      action let b <- testLink.hostResponse.get; r1 <= b.data; last1 <= b.last; endaction
      if (r0 != 0 || last0 || r1 != 1 || !last1)
         $fatal(1, "FAIL: pre-reset input was not admitted");
      pauseCore <= False;
      while (consumed != 1) action noAction; endaction
      pauseCore <= True;
      testLink.hostRequest.put(byteTok(8'h00, True));
      action let b <- testLink.hostResponse.get; r0 <= b.data; last0 <= b.last; endaction
      action let b <- testLink.hostResponse.get; r1 <= b.data; last1 <= b.last; endaction
      if (r0 != 0 || last0 || r1 != 0 || !last1 || consumed != 0)
         $fatal(1, "FAIL: RESET did not clear the application and pending output");
      testLink.hostRequest.put(byteTok(8'h01, True));
      action let b <- testLink.hostResponse.get; r0 <= b.data; last0 <= b.last; endaction
      action let b <- testLink.hostResponse.get; r1 <= b.data; last1 <= b.last; endaction
      if (r0 != 0 || last0 || r1 != 0 || !last1)
         $fatal(1, "FAIL: pre-reset output survived RESET");

      action
         $display("PASS: stream exchange validation, prefix admission, output budget and reset");
         $finish(0);
      endaction
   endseq;

   mkAutoFSM(script);
endmodule

(* synthesize *)
module mkTbGcdStream(Empty);
   TestLink#(8) testLink <- mkTestLink;
   StreamExchange#(Bit#(64), Bit#(32)) stream <- mkStreamExchange(testLink.link);
   Empty gcd <- mkGcdStream(stream);
   Reg#(UInt#(32)) cycles <- mkReg(0);
   Reg#(Bit#(8)) r0 <- mkReg(0);
   Reg#(Bit#(8)) r1 <- mkReg(0);
   Reg#(Bit#(8)) r2 <- mkReg(0);
   Reg#(Bit#(8)) r3 <- mkReg(0);
   Reg#(Bit#(8)) r4 <- mkReg(0);
   Reg#(Bit#(8)) r5 <- mkReg(0);
   Reg#(Bool) last0 <- mkReg(False);
   Reg#(Bool) last1 <- mkReg(False);
   Reg#(Bool) last4 <- mkReg(False);
   Reg#(Bool) last5 <- mkReg(False);

   rule watchdog;
      cycles <= cycles + 1;
      if (cycles == 10000) $fatal(1, "FAIL: GCD stream test timed out");
   endrule

   Stmt script = seq
      testLink.hostRequest.put(byteTok(8'h00, True));
      action let b <- testLink.hostResponse.get; r0 <= b.data; last0 <= b.last; endaction
      action let b <- testLink.hostResponse.get; r1 <= b.data; last1 <= b.last; endaction
      if (r0 != 0 || last0 || r1 != 0 || !last1)
         $fatal(1, "FAIL: GCD stream RESET failed");
      testLink.hostRequest.put(byteTok(8'h01, False));
      testLink.hostRequest.put(byteTok(8'h30, False));
      testLink.hostRequest.put(byteTok(8'h00, False));
      testLink.hostRequest.put(byteTok(8'h00, False));
      testLink.hostRequest.put(byteTok(8'h00, False));
      testLink.hostRequest.put(byteTok(8'h12, False));
      testLink.hostRequest.put(byteTok(8'h00, False));
      testLink.hostRequest.put(byteTok(8'h00, False));
      testLink.hostRequest.put(byteTok(8'h00, True));
      action let b <- testLink.hostResponse.get; r0 <= b.data; last0 <= b.last; endaction
      action let b <- testLink.hostResponse.get; r1 <= b.data; last1 <= b.last; endaction
      if (r0 != 0 || last0 || r1 != 1 || !last1)
         $fatal(1, "FAIL: GCD stream request was not accepted");
      repeat (100) action noAction; endaction
      testLink.hostRequest.put(byteTok(8'h01, True));
      action let b <- testLink.hostResponse.get; r0 <= b.data; last0 <= b.last; endaction
      action let b <- testLink.hostResponse.get; r1 <= b.data; last1 <= b.last; endaction
      action let b <- testLink.hostResponse.get; r2 <= b.data; endaction
      action let b <- testLink.hostResponse.get; r3 <= b.data; endaction
      action let b <- testLink.hostResponse.get; r4 <= b.data; last4 <= b.last; endaction
      action let b <- testLink.hostResponse.get; r5 <= b.data; last5 <= b.last; endaction
      if (r0 != 0 || last0 || r1 != 0 || last1 || {r5, r4, r3, r2} != 32'h00000006
          || !last5)
         $fatal(1, "FAIL: GCD stream did not return gcd(48,18)=6");
      action
         $display("PASS: GCD stream mapped 64-bit request to gcd(48,18)=6");
      endaction
      testLink.hostRequest.put(byteTok(8'h01, False));
      testLink.hostRequest.put(byteTok(8'h00, False));
      testLink.hostRequest.put(byteTok(8'h00, False));
      testLink.hostRequest.put(byteTok(8'h00, False));
      testLink.hostRequest.put(byteTok(8'h00, False));
      testLink.hostRequest.put(byteTok(8'h2a, False));
      testLink.hostRequest.put(byteTok(8'h00, False));
      testLink.hostRequest.put(byteTok(8'h00, False));
      testLink.hostRequest.put(byteTok(8'h00, True));
      action let b <- testLink.hostResponse.get; r0 <= b.data; last0 <= b.last; endaction
      action let b <- testLink.hostResponse.get; r1 <= b.data; last1 <= b.last; endaction
      if (r0 != 0 || last0 || r1 != 1 || !last1)
         $fatal(1, "FAIL: GCD stream rejected zero operand request");
      repeat (100) action noAction; endaction
      testLink.hostRequest.put(byteTok(8'h01, True));
      action let b <- testLink.hostResponse.get; r0 <= b.data; last0 <= b.last; endaction
      action let b <- testLink.hostResponse.get; r1 <= b.data; last1 <= b.last; endaction
      action let b <- testLink.hostResponse.get; r2 <= b.data; endaction
      action let b <- testLink.hostResponse.get; r3 <= b.data; endaction
      action let b <- testLink.hostResponse.get; r4 <= b.data; endaction
      action let b <- testLink.hostResponse.get; r5 <= b.data; last5 <= b.last; endaction
      if (r0 != 0 || last0 || r1 != 0 || last1 || {r5, r4, r3, r2} != 32'h0000002a
          || !last5)
         $fatal(1, "FAIL: GCD stream did not return gcd(0,42)=42");
      action
         $display("PASS: GCD stream handled gcd(0,42)=42");
         $finish(0);
      endaction
   endseq;

   mkAutoFSM(script);
endmodule

// Exercise non-byte-aligned words at the narrow and multi-byte boundaries. The helper also
// checks that the core reset is visible before RESET succeeds and that padding bits are rejected.
module mkTbStreamExchangeNonByte#(Bit#(width) inputWord)(Empty)
   provisos (Add#(widthPad, 1, width));
   TestLink#(8) testLink <- mkTestLink;
   StreamExchange#(Bit#(width), Bit#(width)) stream <- mkStreamExchange(testLink.link);
   Reg#(Bool) pauseCore <- mkReg(True);
   Reg#(Bool) outputReady <- mkReg(False, reset_by stream.coreReset);
   Reg#(Bit#(width)) outputWord <- mkReg(0, reset_by stream.coreReset);
   Reg#(UInt#(32)) cycles <- mkReg(0);
   Reg#(Bit#(8)) statusByte <- mkReg(0);
   Reg#(Bit#(8)) countByte <- mkReg(0);
   Reg#(Bool) statusLast <- mkReg(False);
   Reg#(Bool) countLast <- mkReg(False);
   Integer bytes = (valueOf(width) + 7) / 8;
   Bit#(TMax#(8, width)) paddedInput = zeroExtend(inputWord);

   rule echoCore (!pauseCore);
      let word <- stream.inputWords.get;
      stream.outputWords.put(word);
      outputWord <= word;
      outputReady <= True;
   endrule

   rule watchdog;
      cycles <= cycles + 1;
      if (cycles == 10000) $fatal(1, "FAIL: non-byte stream test timed out");
   endrule

   Stmt script = seq
      testLink.hostRequest.put(byteTok(8'h00, True));
      action
         let status <- testLink.hostResponse.get;
         statusByte <= status.data;
         statusLast <= status.last;
      endaction
      action let count <- testLink.hostResponse.get; countByte <= count.data; countLast <= count.last; endaction
      if (statusByte != 0 || statusLast || countByte != 0 || !countLast)
         $fatal(1, "FAIL: non-byte initial RESET response");
      if (outputReady) $fatal(1, "FAIL: core output state survived initial RESET");

      testLink.hostRequest.put(byteTok(8'h01, False));
      if (bytes == 1)
         testLink.hostRequest.put(byteTok(truncate(paddedInput), True));
      else seq
         testLink.hostRequest.put(byteTok(truncate(paddedInput), False));
         testLink.hostRequest.put(byteTok(truncate(paddedInput >> 8), True));
      endseq
      action
         let status <- testLink.hostResponse.get;
         statusByte <= status.data;
         statusLast <= status.last;
      endaction
      action let count <- testLink.hostResponse.get; countByte <= count.data; countLast <= count.last; endaction
      if (statusByte != 0 || statusLast || countByte != 1 || !countLast)
         $fatal(1, "FAIL: non-byte input word was not admitted");

      pauseCore <= False;
      while (!outputReady) action noAction; endaction
      if (outputWord != inputWord) $fatal(1, "FAIL: non-byte input word changed at core port");
      testLink.hostRequest.put(byteTok(8'h01, True));
      action
         let status <- testLink.hostResponse.get;
         statusByte <= status.data;
         statusLast <= status.last;
      endaction
      action let count <- testLink.hostResponse.get; countByte <= count.data; countLast <= count.last; endaction
      if (statusByte != 0 || statusLast || countByte != 0 || countLast)
         $fatal(1, "FAIL: non-byte output poll header");
      action
         let b <- testLink.hostResponse.get;
         if (b.data != truncate(paddedInput) || b.last != (bytes == 1))
            $fatal(1, "FAIL: non-byte output word low byte changed");
      endaction
      if (bytes != 1)
         action
            let b <- testLink.hostResponse.get;
            if (b.data != truncate(paddedInput >> 8) || !b.last)
               $fatal(1, "FAIL: non-byte output word high byte changed");
         endaction

      testLink.hostRequest.put(byteTok(8'h01, False));
      if (bytes == 1)
         testLink.hostRequest.put(byteTok(8'h01 << (valueOf(width) % 8), True));
      else seq
         testLink.hostRequest.put(byteTok(0, False));
         testLink.hostRequest.put(byteTok(8'h01 << (valueOf(width) % 8), True));
      endseq
      action
         let status <- testLink.hostResponse.get;
         statusByte <= status.data;
         statusLast <= status.last;
      endaction
      action let count <- testLink.hostResponse.get; countByte <= count.data; countLast <= count.last; endaction
      if (statusByte != 1 || statusLast || countByte != 0 || !countLast)
         $fatal(1, "FAIL: non-byte nonzero padding was not rejected");

      pauseCore <= True;
      testLink.hostRequest.put(byteTok(8'h00, True));
      action
         let status <- testLink.hostResponse.get;
         statusByte <= status.data;
         statusLast <= status.last;
      endaction
      action let count <- testLink.hostResponse.get; countByte <= count.data; countLast <= count.last; endaction
      if (statusByte != 0 || statusLast || countByte != 0 || !countLast)
         $fatal(1, "FAIL: non-byte RESET did not complete");
      if (outputReady) $fatal(1, "FAIL: RESET did not clear core output state");
      action
         $display("PASS: non-byte width %0d serialization, padding, and reset", valueOf(width));
         $finish(0);
      endaction
   endseq;

   mkAutoFSM(script);
endmodule

(* synthesize *)
module mkTbStreamExchangeByteAlignedPage4(Empty);
   TestLink#(4) testLink <- mkTestLink;
   StreamExchange#(Bit#(8), Bit#(8)) stream <- mkStreamExchange(testLink.link);
   Reg#(Bool) pauseCore <- mkReg(True);
   Reg#(UInt#(32)) consumed <- mkReg(0);
   Reg#(UInt#(32)) cycles <- mkReg(0);
   Reg#(Bit#(8)) status <- mkReg(0);
   Reg#(Bit#(8)) accepted <- mkReg(0);
   Reg#(Bool) statusLast <- mkReg(False);
   Reg#(Bool) acceptedLast <- mkReg(False);

   rule drainCore (!pauseCore);
      let word <- stream.inputWords.get;
      Bit#(8) expected = consumed < 10
                         ? 8'h20 + truncate(pack(consumed)) : 8'ha0;
      if (word != expected)
         $fatal(1, "FAIL: byte-aligned input order at %0d: got %h expected %h",
                consumed, word, expected);
      consumed <= consumed + 1;
   endrule

   rule watchdog;
      cycles <= cycles + 1;
      if (cycles == 10000) $fatal(1, "FAIL: byte-aligned page-4 test timed out");
   endrule

   Stmt script = seq
      pauseCore <= False;
      testLink.hostRequest.put(byteTok(8'h01, False));
      testLink.hostRequest.put(byteTok(8'h20, False));
      testLink.hostRequest.put(byteTok(8'h21, False));
      testLink.hostRequest.put(byteTok(8'h22, False));
      testLink.hostRequest.put(byteTok(8'h23, False));
      testLink.hostRequest.put(byteTok(8'h24, False));
      testLink.hostRequest.put(byteTok(8'h25, False));
      testLink.hostRequest.put(byteTok(8'h26, False));
      testLink.hostRequest.put(byteTok(8'h27, False));
      testLink.hostRequest.put(byteTok(8'h28, False));
      testLink.hostRequest.put(byteTok(8'h29, True));
      action let b <- testLink.hostResponse.get; status <= b.data; statusLast <= b.last; endaction
      action let b <- testLink.hostResponse.get; accepted <= b.data; acceptedLast <= b.last; endaction
      if (status != 0 || statusLast || accepted != 10 || !acceptedLast)
         $fatal(1, "FAIL: legal page-4 request admitted %0d of 10 byte words", accepted);
      while (consumed != 10) action noAction; endaction

      pauseCore <= True;
      testLink.hostRequest.put(byteTok(8'h01, False));
      testLink.hostRequest.put(byteTok(8'ha0, False));
      testLink.hostRequest.put(byteTok(8'ha1, True));
      action let b <- testLink.hostResponse.get; status <= b.data; statusLast <= b.last; endaction
      action let b <- testLink.hostResponse.get; accepted <= b.data; acceptedLast <= b.last; endaction
      if (status != 0 || statusLast || accepted != 1 || !acceptedLast)
         $fatal(1, "FAIL: partial byte-aligned admission did not report one word");
      pauseCore <= False;
      while (consumed != 11) action noAction; endaction
      action
         $display("PASS: byte-aligned page-4 full and partial ordered admission");
         $finish(0);
      endaction
   endseq;

   mkAutoFSM(script);
endmodule

(* synthesize *)
module mkTbStreamExchangePage8Cap(Empty);
   TestLink#(8) testLink <- mkTestLink;
   StreamExchange#(Bit#(8), Bit#(8)) stream <- mkStreamExchange(testLink.link);
   Reg#(UInt#(9)) consumed <- mkReg(0);
   Reg#(UInt#(32)) cycles <- mkReg(0);
   Reg#(Bit#(8)) status <- mkReg(0);
   Reg#(Bit#(8)) accepted <- mkReg(0);
   Reg#(Bool) statusLast <- mkReg(False);
   Reg#(Bool) acceptedLast <- mkReg(False);

   rule drainCore;
      let word <- stream.inputWords.get;
      Bit#(8) expected = 8'h40;
      if (word != expected)
         $fatal(1, "FAIL: capped page-8 input order at %0d: got %h expected %h",
                consumed, word, expected);
      consumed <= consumed + 1;
   endrule

   rule watchdog;
      cycles <= cycles + 1;
      if (cycles == 10000) $fatal(1, "FAIL: capped page-8 request timed out");
   endrule

   Stmt script = seq
      testLink.hostRequest.put(byteTok(8'h01, False));
      repeat (241) action
         testLink.hostRequest.put(byteTok(8'h40, False));
      endaction
      testLink.hostRequest.put(byteTok(8'h40, True));
      action let b <- testLink.hostResponse.get; status <= b.data; statusLast <= b.last; endaction
      action let b <- testLink.hostResponse.get; accepted <= b.data; acceptedLast <= b.last; endaction
      if (status != 0 || statusLast || accepted != 242 || !acceptedLast)
         $fatal(1, "FAIL: capped page-8 request admitted %0d of 242 byte words", accepted);
      while (consumed != 242) action noAction; endaction
      action
         $display("PASS: page-8 capped frame admits 242 ordered byte words");
         $finish(0);
      endaction
   endseq;

   mkAutoFSM(script);
endmodule

(* synthesize *)
module mkTbStreamExchangePage4W12(Empty);
   TestLink#(4) testLink <- mkTestLink;
   StreamExchange#(Bit#(12), Bit#(12)) stream <- mkStreamExchange(testLink.link);
   Reg#(UInt#(8)) consumed <- mkReg(0);
   Reg#(UInt#(32)) cycles <- mkReg(0);
   Reg#(Bit#(8)) status <- mkReg(0);
   Reg#(Bit#(8)) accepted <- mkReg(0);
   Reg#(Bool) statusLast <- mkReg(False);
   Reg#(Bool) acceptedLast <- mkReg(False);

   rule drainCore;
      let word <- stream.inputWords.get;
      Bit#(12) expected = 12'h120;
      if (word != expected)
         $fatal(1, "FAIL: page-4 fallback order at %0d: got %h expected %h",
                consumed, word, expected);
      consumed <= consumed + 1;
   endrule

   rule watchdog;
      cycles <= cycles + 1;
      if (cycles == 10000) $fatal(1, "FAIL: page-4 fallback request timed out");
   endrule

   Stmt script = seq
      testLink.hostRequest.put(byteTok(8'h01, False));
      repeat (4) seq
         testLink.hostRequest.put(byteTok(8'h20, False));
         testLink.hostRequest.put(byteTok(8'h01, False));
      endseq
      testLink.hostRequest.put(byteTok(8'h20, False));
      testLink.hostRequest.put(byteTok(8'h01, True));
      action let b <- testLink.hostResponse.get; status <= b.data; statusLast <= b.last; endaction
      action let b <- testLink.hostResponse.get; accepted <= b.data; acceptedLast <= b.last; endaction
      if (status != 0 || statusLast || accepted != 1 || !acceptedLast)
         $fatal(1, "FAIL: page-4 fallback did not report one queued prefix word");
      while (consumed != 1) action noAction; endaction
      action
         $display("PASS: page-4 fallback reports its one-word queued prefix");
         $finish(0);
      endaction
   endseq;

   mkAutoFSM(script);
endmodule

(* synthesize *)
module mkTbStreamExchangeW1(Empty);
   Empty test <- mkTbStreamExchangeNonByte(1'b1);
endmodule

(* synthesize *)
module mkTbStreamExchangeW2(Empty);
   Empty test <- mkTbStreamExchangeNonByte(2'b11);
endmodule

(* synthesize *)
module mkTbStreamExchangeW3(Empty);
   Empty test <- mkTbStreamExchangeNonByte(3'b101);
endmodule

(* synthesize *)
module mkTbStreamExchangeW4(Empty);
   Empty test <- mkTbStreamExchangeNonByte(4'hb);
endmodule

(* synthesize *)
module mkTbStreamExchangeW5(Empty);
   Empty test <- mkTbStreamExchangeNonByte(5'h15);
endmodule

(* synthesize *)
module mkTbStreamExchangeW6(Empty);
   Empty test <- mkTbStreamExchangeNonByte(6'h2a);
endmodule

(* synthesize *)
module mkTbStreamExchangeW7(Empty);
   Empty test <- mkTbStreamExchangeNonByte(7'h55);
endmodule

(* synthesize *)
module mkTbStreamExchangeW9(Empty);
   Empty test <- mkTbStreamExchangeNonByte(9'h155);
endmodule

(* synthesize *)
module mkTbStreamExchangeW13(Empty);
   Empty test <- mkTbStreamExchangeNonByte(13'h1abc);
endmodule

endpackage
