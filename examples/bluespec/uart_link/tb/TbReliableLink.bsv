package TbReliableLink;

import StmtFSM :: *;
import Vector :: *;
import BuildVector :: *;
import GetPut :: *;
import UartLinkTypes :: *;
import ReliableLink :: *;

Vector#(19, Bit#(8)) openFrame = vec(
   'h03, 'h62, 'h05, 'h01, 'h03, 'h01, 'h01, 'h01, 'h01, 'h02,
   'hf0, 'h02, 'h08, 'h05, 'hed, 'h45, 'h6a, 'hc4, 0);

Vector#(16, Bit#(8)) nopFrame = vec(
   'h02, 'h40, 'h01, 'h01, 'h03, 'h01, 'h01, 'h01,
   'h01, 'h01, 'h05, 'h4c, 'h41, 'hf3, 'h09, 0);

Vector#(16, Bit#(8)) damagedNopFrame = vec(
   'h02, 'h40, 'h01, 'h01, 'h03, 'h01, 'h01, 'h01,
   'h01, 'h01, 'h05, 'h4c, 'h41, 'hf3, 'h08, 0);

Vector#(15, Bit#(8)) staleAckPoll = vec(
   'h04, 'h60, 'h07, 'hff, 'h03, 'h01, 'h01, 'h01,
   'h01, 'h05, 'hcf, 'h8b, 'h2c, 'he2, 0);

Vector#(16, Bit#(8)) applicationDeliveryAck = vec(
   'h02, 'h40, 'h02, 'h01, 'h03, 'h01, 'h01, 'h01,
   'h01, 'h01, 'h05, 'h6b, 'h3c, 'hcf, 'h40, 0);

Vector#(15, Bit#(8)) recoveryPoll = vec(
   'h04, 'h60, 'h09, 'h01, 'h03, 'h01, 'h01, 'h01,
   'h01, 'h05, 'hb1, 'he6, 'h60, 'h06, 0);

Vector#(16, Bit#(8)) aheadRequest = vec(
   'h03, 'h40, 'h01, 'h01, 'h03, 'h01, 'h01, 'h01,
   'h01, 'h01, 'h05, 'h04, 'h97, 'hcd, 'hfd, 0);

(* synthesize *)
module mkTbReliableLink(Empty);
   ReliableLinkIfc link <- mkReliableLink(240, 8, 256);
   Reg#(UInt#(16)) cycle <- mkReg(0);
   Reg#(UInt#(16)) txBytes <- mkReg(0);
   Reg#(UInt#(8)) txDelimiters <- mkReg(0);
   Reg#(UInt#(16)) i <- mkReg(0);
   Reg#(Bit#(32)) cobsBeforeAbort <- mkReg(0);
   Reg#(UInt#(8)) delimitersBeforeRecovery <- mkReg(0);

   rule watchdog;
      cycle <= cycle + 1;
      if (cycle == 2000) begin
         $display("FAIL: timeout, tx bytes=%0d delimiters=%0d rx=%0d req=%0d replay=%0d crc=%0d len=%0d hdr=%0d gap=%0d drop=%0d",
                  txBytes, txDelimiters, link.stat(0), link.stat(6), link.stat(12), link.stat(8),
                  link.stat(9), link.stat(10), link.stat(11), link.stat(13));
         $fatal(1, "FAIL: reliable-link test timeout");
      end
   endrule

   rule drainTx;
      let value <- link.tx.get;
      txBytes <= txBytes + 1;
      if (value == 0) txDelimiters <= txDelimiters + 1;
   endrule

   Stmt test = seq
      // A corrupted request cannot reach the application service.
      for (i <= 0; i < 16; i <= i + 1) link.rx.put(damagedNopFrame[i]);
      await (link.stat(8) == 1);
      if (link.stat(6) != 0) $fatal(1, "FAIL: bad CRC request was committed");

      // OPEN chooses the largest frame that leaves the fixed 45-byte recovery reserve.
      for (i <= 0; i < 19; i <= i + 1) link.rx.put(openFrame[i]);
      await (txDelimiters == 1);
      if (link.stat(0) != 35)
         $fatal(1, "FAIL: encoded RX byte counter was %0d, expected 35", link.stat(0));
      // The same OPEN token, content, session, and absolute grant must get a fresh reply.
      for (i <= 0; i < 19; i <= i + 1) link.rx.put(openFrame[i]);
      await (txDelimiters == 2);
      if (txBytes != 38) $fatal(1, "FAIL: repeated OPEN did not emit one bounded reply");

      // A request ahead of the expected sequence gets a header-only GAP response.
      for (i <= 0; i < 16; i <= i + 1) link.rx.put(aheadRequest[i]);
      await (txDelimiters == 3);
      if (link.stat(6) != 0 || link.stat(11) != 1)
         $fatal(1, "FAIL: ahead request valid=%0d gap=%0d crc=%0d header=%0d",
                link.stat(6), link.stat(11), link.stat(8), link.stat(10));

      // A valid NOP request reserves one result slot and reaches the service once.
      for (i <= 0; i < 16; i <= i + 1) link.rx.put(nopFrame[i]);
      action
         let request <- link.requestHeader.get;
         if (request.seqNo != 0 || request.length != 1 || request.maxResponse != 196)
            $fatal(1, "FAIL: request metadata was seq=%0d len=%0d max=%0d",
                   request.seqNo, request.length, request.maxResponse);
      endaction
      action
         let value <- link.requestPayload.get;
         if (value != 0) $fatal(1, "FAIL: NOP payload was %02x", value);
      endaction
      link.responseHeader.put(LinkResponseHeader { seqNo: 0, length: 1 });
      link.responsePayload.put('h80);
      await (link.stat(14) == 1);

      // A stale cumulative delivery ACK is rejected with no control reply.
      for (i <= 0; i < 15; i <= i + 1) link.rx.put(staleAckPoll[i]);
      await (link.stat(10) == 1);
      if (txDelimiters != 4 || link.stat(10) != 1)
         $fatal(1, "FAIL: stale control ACK reply=%0d header=%0d",
                txDelimiters, link.stat(10));
      for (i <= 0; i < 16; i <= i + 1) link.rx.put(nopFrame[i]);
      await (txDelimiters == 5);
      await (link.stat(14) == 2);
      if (link.stat(6) != 1 || link.stat(12) != 1)
         $fatal(1, "FAIL: replay dispatched again or was not counted");

      // An application duplicate can carry a delivery ACK and retire its cached result.
      for (i <= 0; i < 16; i <= i + 1) link.rx.put(applicationDeliveryAck[i]);
      repeat (20) noAction;
      if (link.stat(6) != 1 || link.stat(14) != 2)
         $fatal(1, "FAIL: application ACK valid=%0d responseTx=%0d duplicate=%0d",
                link.stat(6), link.stat(14), link.stat(12));

      // An old retry cannot dispatch a result after an application ACK retires it.
      for (i <= 0; i < 16; i <= i + 1) link.rx.put(nopFrame[i]);
      repeat (20) noAction;
      if (link.stat(6) != 1)
         $fatal(1, "FAIL: old sequence executed after delivery retirement");
      if (txBytes > 256)
         $fatal(1, "FAIL: FPGA emitted %0d bytes beyond the fixed bridge grant", txBytes);

      // Abort clears decoder and speculative state, then ignores the affected frame.
      action
         cobsBeforeAbort <= link.stat(7);
         delimitersBeforeRecovery <= txDelimiters;
      endaction
      link.abortRx;
      repeat (1) noAction;
      link.rx.put('h52); link.rx.put('h61); link.rx.put(0);
      repeat (8) noAction;
      if (link.stat(7) != cobsBeforeAbort)
         $fatal(1, "FAIL: post-abort bytes reached COBS decoder");
      for (i <= 0; i < 15; i <= i + 1)
         link.rx.put(recoveryPoll[i]);
      await (txDelimiters == delimitersBeforeRecovery + 1);
      if (link.stat(7) != cobsBeforeAbort)
         $fatal(1, "FAIL: valid frame did not recover cleanly after abort");
      $display("PASS: OPEN, CRC rejection, service streaming, replay, ACK and grant bound");
      $finish(0);
   endseq;
   mkAutoFSM(test);
endmodule

endpackage
