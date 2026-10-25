package TbStreamExchangeOutputReplacement;

import FIFOF :: *;
import GetPut :: *;
import LeanLink :: *;
import StmtFSM :: *;
import StreamExchange :: *;

interface ReplacementTestLink#(numeric type logP);
   interface LeanLink#(0, logP) link;
   method Maybe#(LinkTok) responseEvent;
endinterface

module mkReplacementTestLink(ReplacementTestLink#(logP))
   provisos (Add#(logP, _pad, 8));
   FIFOF#(Bit#(8)) unusedRx <- mkFIFOF;
   FIFOF#(Bit#(8)) unusedTx <- mkFIFOF;
   Reg#(UInt#(2)) requestIndex <- mkReg(0);
   Reg#(UInt#(5)) responseDelay <- mkReg(0);
   Reg#(Bool) responseReady <- mkReg(False);
   RWire#(LinkTok) responseEventWire <- mkRWire(reset_by noReset);

   rule releaseResponse;
      if (responseDelay == 20) responseReady <= True;
      else responseDelay <= responseDelay + 1;
   endrule

   interface LeanLink link;
      interface rx = toPut(unusedRx);
      interface tx = toGet(unusedTx);
      interface Get request;
         method ActionValue#(LinkTok) get if (requestIndex < 3);
            requestIndex <= requestIndex + 1;
            case (requestIndex)
               0: return LinkTok { data: 8'h01, last: False, empty: False };
               1: return LinkTok { data: 8'h31, last: False, empty: False };
               default: return LinkTok { data: 8'h52, last: True, empty: False };
            endcase
         endmethod
      endinterface
      interface Put response;
         method Action put(LinkTok item) if (responseReady);
            responseEventWire.wset(item);
         endmethod
      endinterface
      method Bool responseReady = responseReady;
      method UInt#(8) responseMax = 16;
      method UInt#(8) requestPayloadLength = 3;
   endinterface

   method Maybe#(LinkTok) responseEvent = responseEventWire.wget;
endmodule

(* synthesize *)
module mkTbStreamExchangeOutputReplacement(Empty);
   ReplacementTestLink#(4) testLink <- mkReplacementTestLink;
   StreamExchange#(Bit#(8), Bit#(8)) stream <- mkStreamExchange(testLink.link);
   RWire#(Bool) corePutEvent <- mkRWire(reset_by noReset);
   Reg#(UInt#(32)) cycle <- mkReg(0);
   Reg#(UInt#(8)) corePutCount <- mkReg(0);
   Reg#(UInt#(32)) lastCorePutCycle <- mkReg(0);
   Reg#(UInt#(3)) responseCount <- mkReg(0);
   Reg#(Bit#(8)) status <- mkReg(0);
   Reg#(Bit#(8)) accepted <- mkReg(0);
   Reg#(Bit#(8)) firstOutput <- mkReg(0);
   Reg#(Bit#(8)) secondOutput <- mkReg(0);
   Reg#(Bool) statusLast <- mkReg(False);
   Reg#(Bool) acceptedLast <- mkReg(False);
   Reg#(Bool) firstOutputLast <- mkReg(False);
   Reg#(Bool) secondOutputLast <- mkReg(False);
   Reg#(UInt#(32)) firstOutputByteCycle <- mkReg(0);
   Reg#(UInt#(32)) watchdogCount <- mkReg(0);

   (* fire_when_enabled, no_implicit_conditions *)
   rule monitor;
      cycle <= cycle + 1;
      watchdogCount <= watchdogCount + 1;
      if (watchdogCount == 10000)
         $fatal(1, "FAIL: output replacement test timed out");
      if (cycle == 19
          && (corePutCount != 1 || responseCount != 0 || testLink.link.responseReady))
         $fatal(1, "FAIL: backpressure did not hold a full output queue before release");
      if (isValid(corePutEvent.wget)) begin
         corePutCount <= corePutCount + 1;
         lastCorePutCycle <= cycle;
      end
      if (isValid(testLink.responseEvent)) begin
         let item = fromMaybe(?, testLink.responseEvent);
         if (item.empty) $fatal(1, "FAIL: response emitted an empty token");
         case (responseCount)
            0: begin status <= item.data; statusLast <= item.last; end
            1: begin accepted <= item.data; acceptedLast <= item.last; end
            2: begin
               firstOutput <= item.data;
               firstOutputLast <= item.last;
               firstOutputByteCycle <= cycle;
            end
            default: begin secondOutput <= item.data; secondOutputLast <= item.last; end
         endcase
         responseCount <= responseCount + 1;
      end
   endrule

   rule echoCore;
      let word <- stream.inputWords.get;
      stream.outputWords.put(word + 1);
      corePutEvent.wset(True);
   endrule

   Stmt script = seq
      while (corePutCount != 1) action noAction; endaction
      while (responseCount != 4) action noAction; endaction
      if (status != 0 || statusLast || accepted != 2 || acceptedLast
          || firstOutput != 8'h32 || firstOutputLast
          || secondOutput != 8'h53 || !secondOutputLast)
         $fatal(1, "FAIL: output replacement changed the response bytes or boundaries");
      if (corePutCount != 2 || lastCorePutCycle + 1 != firstOutputByteCycle)
         $fatal(1, "FAIL: second output did not replace the dequeued word in the same cycle");
      $display("PASS: one-entry output replacement preserved data and cycle timing");
      $finish(0);
   endseq;

   mkAutoFSM(script);
endmodule

endpackage
