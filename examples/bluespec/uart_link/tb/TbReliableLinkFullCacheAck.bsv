package TbReliableLinkFullCacheAck;

import StmtFSM :: *;
import Vector :: *;
import BuildVector :: *;
import GetPut :: *;
import UartLinkTypes :: *;
import ReliableLink :: *;

(* synthesize *)
module mkTbReliableLinkFullCacheAck(Empty);
   ReliableLinkIfc link <- mkReliableLink(240, 8, 256);
   Reg#(UInt#(16)) cycle <- mkReg(0);
   Reg#(UInt#(8)) delimiters <- mkReg(0);
   Reg#(UInt#(16)) i <- mkReg(0);
   Reg#(UInt#(4)) requestIndex <- mkReg(0);

   Vector#(19, Bit#(8)) open = vec(
      'h03, 'h62, 'h05, 'h01, 'h03, 'h01, 'h01, 'h01, 'h01, 'h02,
      'hf0, 'h02, 'h08, 'h05, 'hed, 'h45, 'h6a, 'hc4, 0);
   Vector#(9, Vector#(15, Bit#(8))) requests = vec(
      vec('h02, 'h40, 'h01, 'h01, 'h03, 'h01, 'h01, 'h01, 'h01, 'h05, 'h3a, 'h94, 'h73, 'h8f, 0),
      vec('h03, 'h40, 'h01, 'h01, 'h03, 'h01, 'h01, 'h01, 'h01, 'h05, 'h1d, 'he9, 'h4f, 'hc6, 0),
      vec('h03, 'h40, 'h02, 'h01, 'h03, 'h01, 'h01, 'h01, 'h01, 'h05, 'h74, 'h6e, 'h0b, 'h1d, 0),
      vec('h03, 'h40, 'h03, 'h01, 'h03, 'h01, 'h01, 'h01, 'h01, 'h05, 'h53, 'h13, 'h37, 'h54, 0),
      vec('h03, 'h40, 'h04, 'h01, 'h03, 'h01, 'h01, 'h01, 'h01, 'h05, 'h57, 'h16, 'h6e, 'hae, 0),
      vec('h03, 'h40, 'h05, 'h01, 'h03, 'h01, 'h01, 'h01, 'h01, 'h05, 'h70, 'h6b, 'h52, 'he7, 0),
      vec('h03, 'h40, 'h06, 'h01, 'h03, 'h01, 'h01, 'h01, 'h01, 'h05, 'h19, 'hec, 'h16, 'h3c, 0),
      vec('h03, 'h40, 'h07, 'h01, 'h03, 'h01, 'h01, 'h01, 'h01, 'h05, 'h3e, 'h91, 'h2a, 'h75, 0),
      vec('h04, 'h40, 'h08, 'h08, 'h03, 'h01, 'h01, 'h01, 'h01, 'h05, 'hb3, 'h6d, 'h65, 'h83, 0));
   Vector#(15, Bit#(8)) badCrcAck8 = vec(
      'h04, 'h40, 'h08, 'h08, 'h03, 'h01, 'h01, 'h01, 'h01, 'h05,
      'hb3, 'h6d, 'h65, 'h82, 0);
   Vector#(15, Bit#(8)) invalidAck9 = vec(
      'h04, 'h40, 'h08, 'h09, 'h03, 'h01, 'h01, 'h01, 'h01, 'h05,
      'h7b, 'h41, 'h66, 'heb, 0);

   rule watchdog;
      cycle <= cycle + 1;
      if (cycle == 5000) begin
         $display("FAIL: full-cache timeout req=%0d tx=%0d duplicate=%0d drop=%0d hdr=%0d delimiters=%0d",
                  link.stat(6), link.stat(14), link.stat(12), link.stat(13),
                  link.stat(10), delimiters);
         $fatal(1, "FAIL: full-cache ACK test timeout");
      end
   endrule

   rule drainTx;
      let value <- link.tx.get;
      if (value == 0) delimiters <= delimiters + 1;
   endrule

   Stmt test = seq
      for (i <= 0; i < 19; i <= i + 1) link.rx.put(open[i]);
      await (delimiters == 1);

      // Fill every result slot and let each zero-length result leave the FPGA.
      for (requestIndex <= 0; requestIndex < 8; requestIndex <= requestIndex + 1) seq
         for (i <= 0; i < 15; i <= i + 1)
            link.rx.put(requests[requestIndex][i]);
         action
            let request <- link.requestHeader.get;
            if (request.seqNo != zeroExtend(pack(requestIndex)) || request.length != 0)
               $fatal(1, "FAIL: full-cache request metadata mismatch");
         endaction
         link.responseHeader.put(LinkResponseHeader {
            seqNo: zeroExtend(pack(requestIndex)), length: 0
         });
      endseq
      await (link.stat(14) == 8);
      repeat (4) noAction;

      // A bad CRC or an ACK beyond the live window must preserve the cache.
      for (i <= 0; i < 15; i <= i + 1) link.rx.put(badCrcAck8[i]);
      await (link.stat(8) == 1);
      for (i <= 0; i < 15; i <= i + 1) link.rx.put(invalidAck9[i]);
      await (link.stat(10) == 1);
      if (link.stat(6) != 8 || link.stat(14) != 8)
         $fatal(1, "FAIL: invalid ACK or CRC changed cache/dispatch state");

      // A duplicate can still replay the cached result before ACK 8 commits.
      for (i <= 0; i < 15; i <= i + 1) link.rx.put(requests[7][i]);
      await (link.stat(14) == 9);
      if (link.stat(12) != 1 || link.stat(6) != 8)
         $fatal(1, "FAIL: cached result did not survive rejected ACK/CRC");

      // ACK all eight completed results on the ninth request, without a POLL.
      for (i <= 0; i < 15; i <= i + 1) link.rx.put(requests[8][i]);
      action
         let request <- link.requestHeader.get;
         if (request.seqNo != 8 || request.length != 0)
            $fatal(1, "FAIL: full-cache ninth request was not dispatched");
      endaction
      if (link.stat(6) != 9 || link.stat(13) != 0)
         $fatal(1, "FAIL: full-cache ACK dispatch=%0d drops=%0d",
                link.stat(6), link.stat(13));
      $display("PASS: full cache retires by ninth-request ACK without POLL");
      $finish(0);
   endseq;
   mkAutoFSM(test);
endmodule

endpackage
