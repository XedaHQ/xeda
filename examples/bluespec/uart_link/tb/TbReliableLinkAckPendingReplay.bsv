package TbReliableLinkAckPendingReplay;

import StmtFSM :: *;
import Vector :: *;
import BuildVector :: *;
import GetPut :: *;
import UartLinkTypes :: *;
import ReliableLink :: *;

(* synthesize *)
module mkTbReliableLinkAckPendingReplay(Empty);
   ReliableLinkIfc unsent <- mkReliableLink(240, 8, 256);
   ReliableLinkIfc replay <- mkReliableLink(240, 8, 256);
   Reg#(UInt#(16)) cycle <- mkReg(0);
   Reg#(UInt#(16)) i <- mkReg(0);
   Reg#(UInt#(8)) unsentDelimiters <- mkReg(0);
   Reg#(UInt#(8)) replayDelimiters <- mkReg(0);
   Reg#(UInt#(8)) unsentProgress <- mkReg(0);
   Reg#(UInt#(8)) replayProgress <- mkReg(0);
   Reg#(Bool) feedUnsent <- mkReg(False);
   Reg#(Bool) feedReplay <- mkReg(False);

   Vector#(19, Bit#(8)) openFrame = vec(
      'h03, 'h62, 'h05, 'h01, 'h03, 'h01, 'h01, 'h01, 'h01, 'h02,
      'hf0, 'h02, 'h08, 'h05, 'hed, 'h45, 'h6a, 'hc4, 0);
   Vector#(16, Bit#(8)) nopFrame = vec(
      'h02, 'h40, 'h01, 'h01, 'h03, 'h01, 'h01, 'h01,
      'h01, 'h01, 'h05, 'h4c, 'h41, 'hf3, 'h09, 0);
   Vector#(16, Bit#(8)) deliveryAck = vec(
      'h02, 'h40, 'h02, 'h01, 'h03, 'h01, 'h01, 'h01,
      'h01, 'h01, 'h05, 'h6b, 'h3c, 'hcf, 'h40, 0);

   rule watchdog;
      cycle <= cycle + 1;
      if (cycle == 3000) $fatal(1, "FAIL: ACK pending/replay test timeout");
   endrule

   rule drainUnsent;
      let value <- unsent.tx.get;
      if (value == 0) unsentDelimiters <= unsentDelimiters + 1;
   endrule

   rule drainReplay;
      let value <- replay.tx.get;
      if (value == 0) replayDelimiters <= replayDelimiters + 1;
   endrule

   rule feedUnsentResponse (feedUnsent && unsentProgress < 196);
      unsent.responsePayload.put(truncate(pack(unsentProgress)));
      unsentProgress <= unsentProgress + 1;
   endrule

   rule feedReplayResponse (feedReplay && replayProgress < 176);
      replay.responsePayload.put(truncate(pack(replayProgress)));
      replayProgress <= replayProgress + 1;
   endrule

   Stmt test = seq
      for (i <= 0; i < 19; i <= i + 1) action
         unsent.rx.put(openFrame[i]);
         replay.rx.put(openFrame[i]);
      endaction
      await (unsentDelimiters == 1 && replayDelimiters == 1);

      // A complete result that cannot fit the grant must not be retired by ACK 1.
      for (i <= 0; i < 16; i <= i + 1) unsent.rx.put(nopFrame[i]);
      action
         let request <- unsent.requestHeader.get;
         if (request.seqNo != 0 || request.maxResponse != 196)
            $fatal(1, "FAIL: unsent-result request metadata mismatch");
      endaction
      action
         let value <- unsent.requestPayload.get;
         if (value != 0) $fatal(1, "FAIL: unsent-result payload mismatch");
      endaction
      unsent.responseHeader.put(LinkResponseHeader { seqNo: 0, length: 196 });
      feedUnsent <= True;
      await (unsentProgress == 196);
      feedUnsent <= False;
      repeat (8) noAction;
      if (unsent.stat(14) != 0)
         $fatal(1, "FAIL: grant-blocked result was transmitted");
      for (i <= 0; i < 16; i <= i + 1) unsent.rx.put(deliveryAck[i]);
      await (unsent.stat(10) == 1);
      if (unsent.stat(6) != 1 || unsent.stat(14) != 0)
         $fatal(1, "FAIL: ACK retired a result that was never transmitted");

      // After a first transmission, ACK 1 may retire a queued replay.
      for (i <= 0; i < 16; i <= i + 1) replay.rx.put(nopFrame[i]);
      action
         let request <- replay.requestHeader.get;
         if (request.seqNo != 0 || request.maxResponse != 196)
            $fatal(1, "FAIL: replay-result request metadata mismatch");
      endaction
      action
         let value <- replay.requestPayload.get;
         if (value != 0) $fatal(1, "FAIL: replay-result payload mismatch");
      endaction
      replay.responseHeader.put(LinkResponseHeader { seqNo: 0, length: 176 });
      feedReplay <= True;
      await (replayProgress == 176);
      feedReplay <= False;
      await (replay.stat(14) == 1);
      for (i <= 0; i < 16; i <= i + 1) replay.rx.put(nopFrame[i]);
      await (replay.stat(12) == 1);
      repeat (8) noAction;
      if (replay.stat(14) != 1)
         $fatal(1, "FAIL: replay was transmitted despite exhausted grant");
      for (i <= 0; i < 16; i <= i + 1) replay.rx.put(deliveryAck[i]);
      repeat (16) noAction;
      if (replay.stat(10) != 0 || replay.stat(6) != 1)
         $fatal(1, "FAIL: ACK rejected a previously sent result with replay queued");

      $display("PASS: ACK rejects never-sent result and retires queued replay");
      $finish(0);
   endseq;
   mkAutoFSM(test);
endmodule

endpackage
