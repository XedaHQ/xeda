// Directed tests for the packet-to-stream adapter and its GCD attachment.
package TbStreamExchange;

import Clocks :: *;
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
   method Action hostPair(LinkTok first, LinkTok last);
   method UInt#(8) requestPayloadLength;
   method Bool responsePending;
   method UInt#(8) requestTaken;
   method Maybe#(LinkTok) responsePeek;
   method Bool hostRequestReady;
   method Bool hostPairReady;
endinterface

// This packet-boundary double keeps the tests focused on the stream protocol. Serial framing,
// CRC and replay are tested by the LeanLink testbench and the application wire tests.
module mkTestLinkDevice(TestLink#(logP)) provisos (Add#(logP, _pad, 8));
   FIFOF#(LinkTok) requests <- mkSizedFIFOF(256);
   FIFOF#(LinkTok) responses <- mkSizedFIFOF(256);
   FIFOF#(Bit#(8)) unusedRx <- mkFIFOF;
   FIFOF#(Bit#(8)) unusedTx <- mkFIFOF;
   Reg#(UInt#(8)) maxResponse <- mkReg(8);
   Reg#(UInt#(8)) buildingLength <- mkReg(0);
   Reg#(UInt#(8)) requestLength <- mkReg(0);
   Reg#(Bool) requestComplete <- mkReg(False);
   Reg#(UInt#(8)) taken <- mkReg(0);
   Reg#(Bool) pairMode <- mkReg(False);
   Reg#(Bool) pairSecond <- mkReg(False);
   Reg#(LinkTok) pairFirst <- mkRegU;
   Reg#(LinkTok) pairLast <- mkRegU;

   interface LeanLink link;
      interface rx = toPut(unusedRx);
      interface tx = toGet(unusedTx);
      interface Get request;
         method ActionValue#(LinkTok) get if (requestComplete && (pairMode || requests.notEmpty));
            if (pairMode) begin
               if (pairSecond) begin
                  taken <= taken + 1;
                  pairMode <= False;
                  pairSecond <= False;
                  requestComplete <= False;
                  requestLength <= 0;
                  return pairLast;
               end else begin
                  taken <= taken + 1;
                  pairSecond <= True;
                  return pairFirst;
               end
            end else begin
               let item = requests.first;
               requests.deq;
               taken <= taken + 1;
               if (item.empty || item.last) begin
                  requestComplete <= False;
                  requestLength <= 0;
               end
               return item;
            end
         endmethod
      endinterface
      interface response = toPut(responses);
      method Bool responseReady = True;
      method UInt#(8) responseMax = maxResponse;
      method UInt#(8) requestPayloadLength if (requestComplete) = requestLength;
   endinterface
   interface Put hostRequest;
      method Action put(LinkTok item) if (!requestComplete && requests.notFull);
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
   method Action hostPair(LinkTok first, LinkTok last)
      if (!requestComplete && !requests.notEmpty);
      pairFirst <= first;
      pairLast <= last;
      pairMode <= True;
      pairSecond <= False;
      requestLength <= 2;
      requestComplete <= True;
   endmethod
   method UInt#(8) requestPayloadLength if (requestComplete) = requestLength;
   method Bool responsePending = responses.notEmpty;
   method UInt#(8) requestTaken = taken;
   method Maybe#(LinkTok) responsePeek =
      responses.notEmpty ? tagged Valid responses.first : tagged Invalid;
   method Bool hostRequestReady = !requestComplete && requests.notFull;
   method Bool hostPairReady = !requestComplete && !requests.notEmpty;
endmodule

// The test driver stays active while the device's parent reset is asserted. These
// wires carry commands and observations without adding state to either reset domain.
module mkTestLink(TestLink#(logP)) provisos (Add#(logP, _pad, 8));
   TestLink#(logP) device <- mkTestLinkDevice;
   RWire#(LinkTok) requestCommand <- mkRWire(reset_by noReset);
   RWire#(Tuple2#(LinkTok, LinkTok)) pairCommand <- mkRWire(reset_by noReset);
   RWire#(UInt#(8)) limitCommand <- mkRWire(reset_by noReset);
   PulseWire responseTaken <- mkPulseWire(reset_by noReset);
   Wire#(Maybe#(LinkTok)) responseValue <- mkDWire(tagged Invalid, reset_by noReset);
   Wire#(UInt#(8)) takenValue <- mkDWire(0, reset_by noReset);
   Wire#(UInt#(8)) lengthValue <- mkDWire(0, reset_by noReset);
   Wire#(Bool) pendingValue <- mkDWire(False, reset_by noReset);
   Wire#(Bool) requestReady <- mkDWire(False, reset_by noReset);
   Wire#(Bool) pairReady <- mkDWire(False, reset_by noReset);

   rule publishHostReady;
      requestReady <= device.hostRequestReady;
      pairReady <= device.hostPairReady;
   endrule

   rule sendRequest;
      if (isValid(pairCommand.wget)) begin
         let pair = fromMaybe(?, pairCommand.wget);
         device.hostPair(tpl_1(pair), tpl_2(pair));
      end else if (isValid(requestCommand.wget))
         device.hostRequest.put(fromMaybe(?, requestCommand.wget));
   endrule
   rule setLimit (isValid(limitCommand.wget));
      device.setResponseMax(fromMaybe(?, limitCommand.wget));
   endrule
   // A response is visible independently of the driver's acknowledgment. This
   // avoids a ready/acknowledgment loop when the driver waits in a StmtFSM.
   rule observeResponse;
      pendingValue <= device.responsePending;
   endrule
   rule observeTaken;
      takenValue <= device.requestTaken;
   endrule
   rule observeLength;
      lengthValue <= device.requestPayloadLength;
   endrule
   // Peek and acknowledge use separate rules so the value does not depend on ack.
   // The device queue supplies the peek through its observation method.
   rule publishResponse;
      responseValue <= device.responsePeek;
   endrule
   rule acknowledgeResponse (responseTaken);
      let ignored <- device.hostResponse.get;
   endrule

   interface link = device.link;
   interface Put hostRequest;
      method Action put(LinkTok item) if (requestReady);
         requestCommand.wset(item);
      endmethod
   endinterface
   interface Get hostResponse;
      method ActionValue#(LinkTok) get if (isValid(responseValue));
         responseTaken.send;
         return fromMaybe(?, responseValue);
      endmethod
   endinterface
   method Action setResponseMax(UInt#(8) n); limitCommand.wset(n); endmethod
   method Action hostPair(LinkTok first, LinkTok last) if (pairReady);
      pairCommand.wset(tuple2(first, last));
   endmethod
   method UInt#(8) requestPayloadLength = lengthValue;
   method Bool responsePending = pendingValue;
   method UInt#(8) requestTaken = takenValue;
   method Maybe#(LinkTok) responsePeek = responseValue;
   method Bool hostRequestReady = requestReady;
   method Bool hostPairReady = pairReady;
endmodule

function LinkTok byteTok(Bit#(8) b, Bool last) =
   LinkTok { data: b, last: last, empty: False };

(* synthesize *)
module mkTbStreamExchange(Empty);
   TestLink#(8) testLink <- mkTestLink;
   StreamExchange#(Bit#(12), Bit#(12)) stream <- mkStreamExchange(testLink.link);
   Reg#(Bool) pauseCore <- mkReg(True);
   Reg#(UInt#(8)) consumedState <- mkReg(0, reset_by stream.coreReset);
   Wire#(UInt#(8)) consumed <- mkDWire(0, reset_by noReset);
   rule observeConsumed; consumed <= consumedState; endrule
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

   Wire#(Bool) corePaused <- mkDWire(True, reset_by noReset);
   rule publishPause; corePaused <= pauseCore; endrule

   rule echoCore (!corePaused);
      let word <- stream.inputWords.get;
      stream.outputWords.put(truncate(word + 1));
      consumedState <= consumedState + 1;
   endrule

   rule watchdog;
      cycles <= cycles + 1;
      if (cycles == 10000) $fatal(1, "FAIL: stream exchange test timed out");
   endrule

   Stmt script = seq
      // Parent reset must initialize the application before any software RESET arrives.
      testLink.hostRequest.put(byteTok(8'h01, False));
      testLink.hostRequest.put(byteTok(8'h34, False));
      testLink.hostRequest.put(byteTok(8'h02, True));
      action let b <- testLink.hostResponse.get; r0 <= b.data; last0 <= b.last; endaction
      action let b <- testLink.hostResponse.get; r1 <= b.data; last1 <= b.last; endaction
      if (r0 != 0 || last0 || r1 != 1 || !last1 || consumed != 0)
         $fatal(1, "FAIL: parent reset did not initialize the app for first DATA");
      pauseCore <= False;
      while (consumed != 1) action noAction; endaction
      testLink.hostRequest.put(byteTok(8'h01, True));
      action let b <- testLink.hostResponse.get; r0 <= b.data; last0 <= b.last; endaction
      action let b <- testLink.hostResponse.get; r1 <= b.data; last1 <= b.last; endaction
      action let b <- testLink.hostResponse.get; r2 <= b.data; endaction
      action let b <- testLink.hostResponse.get; r3 <= b.data; last3 <= b.last; endaction
      if (r0 != 0 || last0 || r1 != 0 || last1
          || r2 != 8'h35 || r3 != 8'h02 || !last3)
         $fatal(1, "FAIL: first post-parent-reset DATA result was incorrect");
      testLink.hostRequest.put(byteTok(8'h00, True));
      action let b <- testLink.hostResponse.get; r0 <= b.data; last0 <= b.last; endaction
      action let b <- testLink.hostResponse.get; r1 <= b.data; last1 <= b.last; endaction
      if (r0 != 0 || last0 || r1 != 0 || !last1 || consumed != 0)
         $fatal(1, "FAIL: RESET did not clear startup activity");
      pauseCore <= True;

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

      // RESET must flush an accepted input word that the application has not consumed.
      testLink.hostRequest.put(byteTok(8'h00, True));
      action let b <- testLink.hostResponse.get; r0 <= b.data; last0 <= b.last; endaction
      action let b <- testLink.hostResponse.get; r1 <= b.data; last1 <= b.last; endaction
      if (r0 != 0 || last0 || r1 != 0 || !last1 || consumed != 0)
         $fatal(1, "FAIL: RESET did not clear queued input");
      pauseCore <= False;
      repeat (8) action noAction; endaction
      if (consumed != 0) $fatal(1, "FAIL: queued pre-reset input reached the core");
      pauseCore <= True;

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
      if (consumed != 0) $fatal(1, "FAIL: malformed packet reached the core");

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
   Reg#(Bool) outputReadyState <- mkReg(False, reset_by stream.coreReset);
   Wire#(Bool) outputReady <- mkDWire(False, reset_by noReset);
   rule observeOutputReady; outputReady <= outputReadyState; endrule
   Reg#(Bit#(width)) outputWordState <- mkReg(0, reset_by stream.coreReset);
   Wire#(Bit#(width)) outputWord <- mkDWire(0, reset_by noReset);
   rule observeOutputWord; outputWord <= outputWordState; endrule
   Reg#(UInt#(32)) cycles <- mkReg(0);
   Reg#(Bit#(8)) statusByte <- mkReg(0);
   Reg#(Bit#(8)) countByte <- mkReg(0);
   Reg#(Bool) statusLast <- mkReg(False);
   Reg#(Bool) countLast <- mkReg(False);
   Integer bytes = (valueOf(width) + 7) / 8;
   Bit#(TMax#(8, width)) paddedInput = zeroExtend(inputWord);

   Wire#(Bool) corePaused <- mkDWire(True, reset_by noReset);
   rule publishPause; corePaused <= pauseCore; endrule

   rule echoCore (!corePaused);
      let word <- stream.inputWords.get;
      stream.outputWords.put(word);
      outputWordState <= word;
      outputReadyState <= True;
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
   Reg#(UInt#(32)) consumedState <- mkReg(0, reset_by stream.coreReset);
   Wire#(UInt#(32)) consumed <- mkDWire(0, reset_by noReset);
   rule observeConsumed; consumed <= consumedState; endrule
   Reg#(UInt#(32)) cycles <- mkReg(0);
   Reg#(Bit#(8)) status <- mkReg(0);
   Reg#(Bit#(8)) accepted <- mkReg(0);
   Reg#(Bool) statusLast <- mkReg(False);
   Reg#(Bool) acceptedLast <- mkReg(False);

   Reg#(UInt#(32)) previousConsumeCycle <- mkReg(0, reset_by stream.coreReset);

   Wire#(Bool) corePaused <- mkDWire(True, reset_by noReset);
   Wire#(UInt#(32)) coreCycle <- mkDWire(0, reset_by noReset);
   rule publishPause; corePaused <= pauseCore; coreCycle <= cycles; endrule

   rule drainCore (!corePaused);
      let word <- stream.inputWords.get;
      Bit#(8) expected = consumedState < 10
                         ? 8'h20 + truncate(pack(consumedState)) : 8'ha0;
      if (word != expected)
         $fatal(1, "FAIL: byte-aligned input order at %0d: got %h expected %h",
                consumedState, word, expected);
      if (consumedState > 0 && consumedState < 10 && coreCycle != previousConsumeCycle + 1)
         $fatal(1, "FAIL: byte-aligned admission has a bubble at word %0d, delta=%0d",
                consumedState, coreCycle - previousConsumeCycle);
      if (consumedState == 0 || consumedState == 9)
         $display("METRIC: byte word %0d consumed at cycle %0d", consumedState, coreCycle);
      previousConsumeCycle <= coreCycle;
      consumedState <= consumedState + 1;
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
   Reg#(UInt#(9)) consumedState <- mkReg(0, reset_by stream.coreReset);
   Wire#(UInt#(9)) consumed <- mkDWire(0, reset_by noReset);
   rule observeConsumed; consumed <= consumedState; endrule
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
                consumedState, word, expected);
      consumedState <= consumedState + 1;
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
   Reg#(UInt#(8)) consumedState <- mkReg(0, reset_by stream.coreReset);
   Wire#(UInt#(8)) consumed <- mkDWire(0, reset_by noReset);
   rule observeConsumed; consumed <= consumedState; endrule
   Reg#(UInt#(32)) cycles <- mkReg(0);
   Reg#(Bit#(8)) status <- mkReg(0);
   Reg#(Bit#(8)) accepted <- mkReg(0);
   Reg#(Bool) statusLast <- mkReg(False);
   Reg#(Bool) acceptedLast <- mkReg(False);

   Reg#(Bool) pauseCore <- mkReg(True);
   Wire#(Bool) corePaused <- mkDWire(True, reset_by noReset);
   rule publishPause; corePaused <= pauseCore; endrule

   rule drainCore (!corePaused);
      let word <- stream.inputWords.get;
      Bit#(12) expected = 12'h120;
      if (word != expected)
         $fatal(1, "FAIL: page-4 fallback order at %0d: got %h expected %h",
                consumedState, word, expected);
      consumedState <= consumedState + 1;
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
      pauseCore <= False;
      while (consumed != 1) action noAction; endaction

      // An available consumer permits all five words, including replacement of a
      // full queue at the admission decision. The accepted count must not wrap.
      testLink.hostRequest.put(byteTok(8'h01, False));
      repeat (4) seq
         testLink.hostRequest.put(byteTok(8'h20, False));
         testLink.hostRequest.put(byteTok(8'h01, False));
      endseq
      testLink.hostRequest.put(byteTok(8'h20, False));
      testLink.hostRequest.put(byteTok(8'h01, True));
      action let b <- testLink.hostResponse.get; status <= b.data; statusLast <= b.last; endaction
      action let b <- testLink.hostResponse.get; accepted <= b.data; acceptedLast <= b.last; endaction
      if (status != 0 || statusLast || accepted != 5 || !acceptedLast)
         $fatal(1, "FAIL: page-4 fallback did not admit all five words to a ready consumer");
      while (consumed != 6) action noAction; endaction
      action
         $display("PASS: page-4 fallback admits one stalled word and five ready words in order");
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


(* synthesize *)
module mkTbResetInterrupt(Empty);
   Clock clk <- exposeCurrentClock;
   MakeResetIfc dutParent <- mkReset(0, True, clk);
   TestLink#(8) testLink <- mkTestLink(reset_by dutParent.new_rst);
   StreamExchange#(Bit#(8), Bit#(8)) stream <- mkStreamExchange(
      testLink.link, clocked_by clk, reset_by dutParent.new_rst);
   ReadOnly#(Bool) dutResetActive <- isResetAssertedDirect(reset_by stream.coreReset);
   ReadOnly#(Bool) parentResetActive <- isResetAssertedDirect(reset_by dutParent.new_rst);
   Reg#(UInt#(8)) consumedState <- mkReg(0, reset_by stream.coreReset);
   Wire#(UInt#(8)) consumed <- mkDWire(0, reset_by noReset);
   rule observeConsumed; consumed <= consumedState; endrule
   Reg#(UInt#(32)) cycles <- mkReg(0);

   rule echoCore;
      let word <- stream.inputWords.get;
      stream.outputWords.put(truncate(word + 1));
      consumedState <= consumedState + 1;
   endrule

   rule watchdog;
      cycles <= cycles + 1;
      if (cycles == 30000) $fatal(1, "FAIL: parent reset interruption test timed out");
   endrule

   Stmt script = seq
      repeat (8) action noAction; endaction
      testLink.hostRequest.put(byteTok(8'h00, True));
      while (!dutResetActive) action noAction; endaction
      action
         if (!dutResetActive) $fatal(1, "FAIL: software reset pulse ended before parent interrupt phase 0");
         dutParent.assertReset;
      endaction
      while (!parentResetActive) action noAction; endaction
      while (parentResetActive) action noAction; endaction
      if (!dutResetActive) $fatal(1, "FAIL: child reset did not overlap parent reset release");
      while (testLink.responsePending) action
         let stale <- testLink.hostResponse.get;
      endaction

      testLink.hostPair(byteTok(8'h01, False), byteTok(8'h5a, True));
      if (!dutResetActive) $fatal(1, "FAIL: child reset ended before DATA became ready");
      repeat (1) action noAction; endaction
      if (testLink.requestTaken != 0)
         $fatal(1, "FAIL: request consumed while child reset remained active");
      while (dutResetActive) action noAction; endaction
      action let b <- testLink.hostResponse.get;
         if (b.data != 0 || b.last) $fatal(1, "FAIL: DATA status after parent reset");
      endaction
      action let b <- testLink.hostResponse.get;
         if (b.data != 1 || b.empty || b.last)
            $fatal(1, "FAIL: DATA count after parent reset");
      endaction
      action let b <- testLink.hostResponse.get;
         if (b.data != 8'h5b || b.empty || !b.last)
            $fatal(1, "FAIL: stale or incorrect output survived parent reset");
      endaction
      while (testLink.requestTaken != 2) action noAction; endaction
      while (consumed != 1) action noAction; endaction
      testLink.hostRequest.put(byteTok(8'h00, True));
      while (!dutResetActive) action noAction; endaction
      repeat (1) action noAction; endaction
      action
         if (!dutResetActive) $fatal(1, "FAIL: software reset pulse ended before parent interrupt phase 1");
         dutParent.assertReset;
      endaction
      while (!parentResetActive) action noAction; endaction
      while (parentResetActive) action noAction; endaction
      if (!dutResetActive) $fatal(1, "FAIL: child reset did not overlap parent reset release");
      while (testLink.responsePending) action
         let stale <- testLink.hostResponse.get;
      endaction

      testLink.hostPair(byteTok(8'h01, False), byteTok(8'h6b, True));
      if (!dutResetActive) $fatal(1, "FAIL: child reset ended before DATA became ready");
      repeat (1) action noAction; endaction
      if (testLink.requestTaken != 0)
         $fatal(1, "FAIL: request consumed while child reset remained active");
      while (dutResetActive) action noAction; endaction
      action let b <- testLink.hostResponse.get;
         if (b.data != 0 || b.last) $fatal(1, "FAIL: DATA status after parent reset");
      endaction
      action let b <- testLink.hostResponse.get;
         if (b.data != 1 || b.empty || b.last)
            $fatal(1, "FAIL: DATA count after parent reset");
      endaction
      action let b <- testLink.hostResponse.get;
         if (b.data != 8'h6c || b.empty || !b.last)
            $fatal(1, "FAIL: stale or incorrect output survived parent reset");
      endaction
      while (testLink.requestTaken != 2) action noAction; endaction
      while (consumed != 1) action noAction; endaction
      testLink.hostRequest.put(byteTok(8'h00, True));
      while (!dutResetActive) action noAction; endaction
      repeat (2) action noAction; endaction
      action
         if (!dutResetActive) $fatal(1, "FAIL: software reset pulse ended before parent interrupt phase 2");
         dutParent.assertReset;
      endaction
      while (!parentResetActive) action noAction; endaction
      while (parentResetActive) action noAction; endaction
      if (!dutResetActive) $fatal(1, "FAIL: child reset did not overlap parent reset release");
      while (testLink.responsePending) action
         let stale <- testLink.hostResponse.get;
      endaction

      testLink.hostPair(byteTok(8'h01, False), byteTok(8'h7c, True));
      if (!dutResetActive) $fatal(1, "FAIL: child reset ended before DATA became ready");
      repeat (1) action noAction; endaction
      if (testLink.requestTaken != 0)
         $fatal(1, "FAIL: request consumed while child reset remained active");
      while (dutResetActive) action noAction; endaction
      action let b <- testLink.hostResponse.get;
         if (b.data != 0 || b.last) $fatal(1, "FAIL: DATA status after parent reset");
      endaction
      action let b <- testLink.hostResponse.get;
         if (b.data != 1 || b.empty || b.last)
            $fatal(1, "FAIL: DATA count after parent reset");
      endaction
      action let b <- testLink.hostResponse.get;
         if (b.data != 8'h7d || b.empty || !b.last)
            $fatal(1, "FAIL: stale or incorrect output survived parent reset");
      endaction
      while (testLink.requestTaken != 2) action noAction; endaction
      while (consumed != 1) action noAction; endaction
      testLink.hostRequest.put(byteTok(8'h00, True));
      while (!dutResetActive) action noAction; endaction
      repeat (3) action noAction; endaction
      action
         if (!dutResetActive) $fatal(1, "FAIL: software reset pulse ended before parent interrupt phase 3");
         dutParent.assertReset;
      endaction
      while (!parentResetActive) action noAction; endaction
      while (parentResetActive) action noAction; endaction
      if (!dutResetActive) $fatal(1, "FAIL: child reset did not overlap parent reset release");
      while (testLink.responsePending) action
         let stale <- testLink.hostResponse.get;
      endaction

      testLink.hostPair(byteTok(8'h01, False), byteTok(8'h8d, True));
      if (!dutResetActive) $fatal(1, "FAIL: child reset ended before DATA became ready");
      repeat (1) action noAction; endaction
      if (testLink.requestTaken != 0)
         $fatal(1, "FAIL: request consumed while child reset remained active");
      while (dutResetActive) action noAction; endaction
      action let b <- testLink.hostResponse.get;
         if (b.data != 0 || b.last) $fatal(1, "FAIL: DATA status after parent reset");
      endaction
      action let b <- testLink.hostResponse.get;
         if (b.data != 1 || b.empty || b.last)
            $fatal(1, "FAIL: DATA count after parent reset");
      endaction
      action let b <- testLink.hostResponse.get;
         if (b.data != 8'h8e || b.empty || !b.last)
            $fatal(1, "FAIL: stale or incorrect output survived parent reset");
      endaction
      while (testLink.requestTaken != 2) action noAction; endaction
      while (consumed != 1) action noAction; endaction
      action
         $display("PASS: parent reset interrupts software reset in four phases");
         $finish(0);
      endaction
   endseq;

   mkAutoFSM(script);
endmodule

// Exercise the optimized five-byte length decoder through the actual adapter.
// Every byte-sized payload length must agree with the word-boundary contract.
(* synthesize *)
module mkTbStreamExchangeWord5Lengths(Empty);
   TestLink#(8) testLink <- mkTestLink;
   StreamExchange#(Bit#(40), Bit#(40)) stream <- mkStreamExchange(testLink.link);
   Reg#(UInt#(16)) consumedState <- mkReg(0, reset_by stream.coreReset);
   Wire#(UInt#(16)) consumed <- mkDWire(0, reset_by noReset);
   rule observeConsumed; consumed <= consumedState; endrule
   Reg#(UInt#(16)) expectedTotal <- mkReg(0);
   Reg#(UInt#(9)) length <- mkReg(0);
   Reg#(UInt#(9)) position <- mkReg(0);
   Reg#(UInt#(32)) cycles <- mkReg(0);
   Reg#(LinkTok) status <- mkRegU;
   Reg#(LinkTok) count <- mkRegU;

   rule consumeWord;
      let word <- stream.inputWords.get;
      if (word != 0) $fatal(1, "FAIL: five-byte length test changed an input word");
      consumedState <= consumedState + 1;
   endrule
   rule watchdog;
      cycles <= cycles + 1;
      if (cycles == 100000) $fatal(1, "FAIL: five-byte length test timed out");
   endrule

   Stmt script = seq
      testLink.setResponseMax(2);
      repeat (256) seq
         if (length == 0)
            testLink.hostRequest.put(LinkTok { data: 0, empty: True, last: True });
         else seq
            testLink.hostRequest.put(byteTok(1, length == 1));
            position <= 1;
            while (position < length) seq
               testLink.hostRequest.put(byteTok(0, position + 1 == length));
               position <= position + 1;
            endseq
         endseq
         action let b <- testLink.hostResponse.get; status <= b; endaction
         action let b <- testLink.hostResponse.get; count <= b; endaction
         action
            Bool valid = length >= 1 && (length - 1) % 5 == 0;
            UInt#(8) expected = valid ? truncate((length - 1) / 5) : 0;
            if (status.data != (valid ? 0 : 1) || status.empty || status.last
                || count.data != pack(expected) || count.empty || !count.last
                || consumed != expectedTotal + zeroExtend(expected))
               $fatal(1, "FAIL: five-byte payload length %0d, status=%0d count=%0d consumed=%0d",
                      length, status.data, count.data, consumed);
            expectedTotal <= expectedTotal + zeroExtend(expected);
            length <= length + 1;
         endaction
      endseq
      action
         $display("PASS: all 256 five-byte payload lengths match the word-boundary contract");
         $finish(0);
      endaction
   endseq;
   mkAutoFSM(script);
endmodule

endpackage
