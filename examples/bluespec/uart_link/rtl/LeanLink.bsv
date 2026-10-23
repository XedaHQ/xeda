// LeanLink: a small reliable packet transport over a byte stream.
//
// Host frames use COBS, FPGA frames use streaming reverse COBS; both end with a zero byte. Each
// frame is one packet with a 6-byte header, a payload and a trailer of CRC-32C XOR session ID.
// Requests and responses live in pages indexed by sequence number mod W, so all window state is a
// few counters. See doc/protocol.md for the protocol. The shared counters and flags have one
// writer, rule updState. The other registers are written by rules and methods whose conditions
// exclude each other.
package LeanLink;

import BRAMCore :: *;
import Clocks :: *;
import ConfigReg :: *;
import GetPut :: *;
import RegFile :: *;

export LinkTok(..);
export LeanLink(..);
export mkLeanLink;

// One byte of a packet. A zero-length packet is one token with `empty` and `last`.
typedef struct {
   Bit#(8) data;
   Bool last;
   Bool empty;
} LinkTok deriving (Bits, Eq, FShow);

interface LeanLink#(numeric type logW, numeric type logP);
   interface Put#(Bit#(8)) rx;          // encoded bytes from the byte transport
   interface Get#(Bit#(8)) tx;          // encoded bytes to the byte transport
   interface Get#(LinkTok) request;     // validated requests, whole and in order
   method UInt#(8) requestPayloadLength;
   interface Put#(LinkTok) response;    // one response per request, in request order
   (* always_ready *) method Bool responseReady;
   (* always_ready *) method UInt#(8) responseMax;
endinterface

Bit#(32) crcPoly = 32'h82F63B78;

typedef enum { RdIdle, RdWaitLen, RdLen, RdWait, RdData, RdEmpty } RdState deriving (Bits, Eq);
typedef enum { THdr, TBody, TTrl, TFin } TxPhase deriving (Bits, Eq);

module mkLeanLink(LeanLink#(logW, logP))
   provisos (Add#(logW, 1, sb), Add#(logW, logP, ab), Add#(sb, _a, 8), Add#(logP, _b, 8),
             Add#(logW, _c, sb));
   Integer w = valueOf(TExp#(logW));
   Integer p = valueOf(TExp#(logP));
   Bit#(8) maxLen = fromInteger(min(255, p + 7));
   Bit#(3) logWBits = fromInteger(valueOf(logW));

   if (valueOf(logW) > 7)
      error("mkLeanLink: W is at most 128 (the sequence fields are one byte, modulo 2W)");
   if (valueOf(logP) < 4)
      error("mkLeanLink: pages need at least 16 bytes (header offsets 6 to 11, payload from 12)");

   function Bit#(logW) pg(Bit#(sb) s) = truncate(s);
   function Bit#(logP) off(Bit#(8) o) = truncate(o);
   function Bool inWin(Bit#(sb) a, Bit#(sb) b) = (a - b) < fromInteger(w);

   BRAM_DUAL_PORT#(Bit#(ab), Bit#(8)) reqRam <- mkBRAMCore2(w * p, False);
   BRAM_DUAL_PORT#(Bit#(ab), Bit#(8)) rspRam <- mkBRAMCore2(w * p, False);
   RegFile#(Bit#(logW), Bit#(9)) lenTbl <- mkRegFileWCF(0, fromInteger(w - 1));   // {TRUNC, len}

   // ---------------- Session and window state ----------------
   // A register that a session reset (or another event) clears is mkRegU and is cleared
   // in its one always-firing writer by `event || inRst`: one constant source, so the
   // clear maps to the flip-flop reset pin instead of a LUT per bit.
   ReadOnly#(Bool) inRst <- isResetAssertedDirect;   // the module reset as a value, combinational
   Reg#(Bit#(32)) sid <- mkConfigRegU;
   Reg#(Bool) bound <- mkConfigReg(False);
   // Session generation: toggles at each session reset. An RX frame that started in another
   // generation never commits, so no frame straddles a reset.
   Reg#(Bool) sessGen <- mkConfigReg(False);
   Reg#(Bit#(8)) rspLimit <- mkConfigRegU;
   Reg#(Bit#(32)) pendSid <- mkRegU;
   Reg#(Bit#(8)) pendLimit <- mkRegU;
   Reg#(Bool) openPending <- mkReg(False);
   Reg#(Bool) owe <- mkReg(False);
   // RESYNC: the host saw no FPGA bytes for a quiet period; one status frame may go without credit.
   Reg#(Bool) permit <- mkReg(False);
   Reg#(Bit#(2)) rwHost <- mkReg(0);
   Reg#(Bit#(2)) rwDone <- mkReg(0);
   // Boot grant: exactly one 12-byte status frame inside the 64-byte status reserve.
   Reg#(Bit#(16)) grant <- mkConfigReg(16'hFFCC);
   Reg#(Bit#(16)) cursor <- mkRegU;
   Reg#(Bit#(sb)) exp <- mkRegU;
   Reg#(Bit#(sb)) dack <- mkRegU;
   Reg#(Bit#(sb)) cons <- mkRegU;
   Reg#(Bit#(sb)) wr <- mkRegU;
   Reg#(Bit#(sb)) txq <- mkRegU;
   Reg#(Bit#(sb)) hi <- mkRegU;

   PulseWire resetW <- mkPulseWire;
   PulseWire commitW <- mkPulseWire;
   RWire#(Bit#(sb)) dackW <- mkRWire;
   PulseWire consIncW <- mkPulseWire;
   PulseWire wrIncW <- mkPulseWire;
   PulseWire txLoadW <- mkPulseWire;
   PulseWire txEndW <- mkPulseWire;
   PulseWire setOweW <- mkPulseWire;
   PulseWire txSetOweW <- mkPulseWire;
   PulseWire txClrOweW <- mkPulseWire;
   PulseWire setOpenW <- mkPulseWire;
   RWire#(Bit#(16)) cursorW <- mkRWire;                 // TX start: cursor := next
   PulseWire trySetW <- mkPulseWire;
   PulseWire tryClrW <- mkPulseWire;
   PulseWire wLastW <- mkPulseWire;                     // response writer: last token
   RWire#(Tuple2#(Bit#(8), Bool)) wByteW <- mkRWire;    // response writer: {offset, trunc}
   PulseWire resyncW <- mkPulseWire;
   PulseWire txStatusW <- mkPulseWire;
   PulseWire grantWrW <- mkPulseWire;      // grant written this clock
   PulseWire lenWrW <- mkPulseWire;        // lenTbl written this clock

   // ---------------- RX framer ----------------
   Reg#(Bool) rxAny <- mkReg(False);         // bytes seen since the last delimiter
   Reg#(Bit#(8)) cobsCode <- mkRegU;         // the COBS code byte of the current block
   Reg#(Bit#(8)) cnt <- mkRegU;              // 1 + data bytes received in the block
   Reg#(Bool) zeroPend <- mkRegU;            // the next code byte implies a zero first
   // The code byte c is followed by c - 1 data bytes, so the byte that arrives with
   // cnt == cobsCode is the next code byte. The compare is one 8-bit equality, with no load
   // subtractor and no load multiplexer.
   Bool blockEnd = cnt == cobsCode;
   Reg#(Bit#(8)) pos <- mkRegU;              // decoded byte index + 6 = page offset
   Reg#(Bool) inHdr <- mkRegU;
   Reg#(Bool) inTr <- mkRegU;
   Reg#(Bit#(2)) tcnt <- mkRegU;
   Reg#(Bool) done <- mkRegU;                // all four trailer bytes seen
   Reg#(Bool) over <- mkRegU;                // a decoded byte after the trailer
   Reg#(Bool) landOK <- mkRegU;              // this frame may write request page exp mod W
   Reg#(Bool) rxGen <- mkRegU;               // session generation at the frame's first byte
   Reg#(Bit#(48)) hdr <- mkRegU;
   Reg#(Bit#(40)) pl <- mkRegU;              // last five payload bytes (OPEN payload)
   Reg#(Bit#(32)) crc <- mkRegU;
   Reg#(Bit#(8)) hb <- mkRegU;
   Reg#(Bit#(3)) idx <- mkReg(0);
   Reg#(Bool) crcRun <- mkReg(False);
   Reg#(Bool) trMode <- mkRegU;
   Reg#(Bit#(2)) tsel <- mkRegU;
   Reg#(Bool) bad <- mkRegU;

   Bit#(8) hCtl = hdr[7:0];
   Bit#(8) hSeq = hdr[15:8];
   Bit#(8) hAck = hdr[23:16];
   Bit#(16) hGrant = hdr[39:24];
   Bit#(8) hLen = hdr[47:40];
   Bool hOpen = hCtl[5] == 1;
   Bool hData = hCtl[4] == 1;
   Bool hPoll = hCtl[3] == 1;
   Bool hResync = hCtl[0] == 1;

   Bit#(1) sBitRx = (hOpen || !bound) ? 0 : sid[{tsel, idx}];

   (* fire_when_enabled *)
   rule rxCrcStep (crcRun);
      Bit#(1) d = hb[idx];
      Bit#(1) fb = trMode ? 0 : crc[0] ^ d;
      crc <= (crc >> 1) ^ (fb == 1 ? crcPoly : 0);
      // The trailer is the complemented CRC XOR the session ID, least significant bit first.
      if (trMode && d != (~crc[0] ^ sBitRx)) bad <= True;
      idx <= idx + 1;
      if (idx == 7) crcRun <= False;
   endrule

   function Action crcLoad(Bit#(8) d, Bool trl, Bit#(2) ts) = action
      hb <= d; trMode <= trl; tsel <= ts; crcRun <= True;
   endaction;

   function Action decoded(Bit#(8) d) = action
      if (landOK) reqRam.a.put(True, {pg(exp), off(pos)}, d);
      pos <= pos + 1;
      if (done) over <= True;
      if (inHdr) begin
         hdr <= {d, hdr[47:8]};
         if (pos == 11) inHdr <= False;
         crcLoad(d, False, ?);
      end
      else if (inTr || pos == hLen) begin
         inTr <= True;
         tcnt <= tcnt + 1;
         if (tcnt == 3) done <= True;
         crcLoad(d, True, tcnt);
      end
      else begin
         pl <= {d, pl[39:8]};
         crcLoad(d, False, ?);
      end
   endaction;

   Bit#(sb) hAckS = truncate(hAck);
   Bit#(sb) hSeqS = truncate(hSeq);

   function Action decide = action
      Bool fmtOk = blockEnd && done && !over && !bad && rxGen == sessGen
         && hCtl[7:6] == 2'b10
         && (hSeq >> valueOf(sb)) == 0 && (hAck >> valueOf(sb)) == 0
         && hLen >= 12 && hLen <= maxLen
         && (hOpen ? (!hData && hLen == 17 && pl[39:32] >= 12)
                   : (hData || hLen == 12));
      Bool ackOk = (hAckS - dack) <= (hi - dack);
      Bool commit = hData && hSeqS == exp && landOK;
      if (!fmtOk) begin
         if (!bound) setOweW.send;
      end
      else if (hOpen) begin
         pendSid <= pl[31:0]; pendLimit <= pl[39:32]; grant <= hGrant; grantWrW.send;
         setOpenW.send;
      end
      else if (!bound) setOweW.send;
      else if (!openPending && ackOk) begin
         dackW.wset(hAckS); grant <= hGrant; grantWrW.send; rwHost <= hCtl[2:1];
         if (commit) commitW.send;
         if ((hData && !commit) || hPoll || hResync) setOweW.send;
         if (hResync) resyncW.send;
      end
   endaction;

   // ---------------- Request reader ----------------
   Reg#(RdState) rdState <- mkReg(RdIdle);
   // `ro` is 11 (the length byte's offset) whenever the reader is idle: it returns to 11 at the
   // end of a request, while a request is abandoned, and in module reset (rule roUpd).
   Reg#(Bit#(8)) ro <- mkRegU;
   PulseWire roIncW <- mkPulseWire;
   PulseWire roRstW <- mkPulseWire;
   Reg#(Bit#(8)) rlast <- mkRegU;           // page offset of the request's last byte
   Reg#(Bool) rdStarted <- mkReg(False);     // the core took a token of the current request
   // During OPEN, a request the core has not started is abandoned (and the reset discards it).
   Bool rdAbandon = openPending && !rdStarted;

   (* fire_when_enabled, no_implicit_conditions *)
   rule rdRam;
      reqRam.b.put(False, {pg(cons), off(ro)}, ?);
   endrule

   (* fire_when_enabled, no_implicit_conditions *)
   rule roUpd;
      ro <= (roRstW || inRst) ? 11 : (roIncW ? ro + 1 : ro);
   endrule

   (* fire_when_enabled *)
   rule rdStep ((rdState != RdData && rdState != RdEmpty) || rdAbandon);
      if (rdAbandon) begin rdState <= RdIdle; roRstW.send; end
      else case (rdState)
         RdIdle: if (cons != exp && !openPending) rdState <= RdWaitLen;   // ro is 11
         RdWaitLen: rdState <= RdLen;
         RdLen: begin
            Bit#(8) l = reqRam.b.read;
            rlast <= l - 1; roIncW.send;  // 11 -> 12
            rdState <= l == 12 ? RdEmpty : RdWait;
         end
         RdWait: rdState <= RdData;
      endcase
   endrule

   // ---------------- Response writer ----------------
   Reg#(Bit#(8)) wo <- mkRegU;
   Reg#(Bool) wTrunc <- mkRegU;
   Reg#(Bool) wActive <- mkRegU;

   // The writer state returns to its initial values at a response's last token and in module
   // reset (one writer, so the clear maps to the flip-flop reset pin).
   (* fire_when_enabled, no_implicit_conditions *)
   rule wUpd;
      Bool clr = wLastW || inRst;
      match {.o, .tr} = fromMaybe(tuple2(wo, wTrunc), wByteW.wget);
      wo <= clr ? 12 : o;
      wTrunc <= clr ? False : tr;
      wActive <= clr ? False : (isValid(wByteW.wget) ? True : wActive);
   endrule

   // ---------------- TX framer ----------------
   Reg#(Bool) txActive <- mkConfigReg(False);
   Reg#(Bool) txIsData <- mkConfigRegU;
   Reg#(Bit#(8)) t <- mkRegU;                // decoded byte index + 6 = page offset
   Reg#(Bit#(8)) vlen <- mkRegU;             // wire length of the frame in progress
   Reg#(TxPhase) phase <- mkRegU;
   Reg#(Bit#(2)) tcntT <- mkRegU;
   Reg#(Bit#(32)) crcT <- mkRegU;
   Reg#(Bit#(8)) tb <- mkRegU;               // assembled one bit per clock, LSB first
   Reg#(Bool) tbFull <- mkReg(False);
   Reg#(Bit#(3)) idxT <- mkReg(0);           // bit index of the byte in assembly
   Reg#(Bit#(sb)) expT <- mkRegU;            // exp sampled at the first bit of a header byte
   Reg#(Bit#(8)) code <- mkRegU;             // reverse COBS run length + 1
   // After reset the first byte out is a delimiter. It ends any partial frame that the host
   // still holds from before the reset (for example from an earlier FPGA configuration).
   Reg#(Bit#(8)) ob <- mkReg(0);
   Reg#(Bool) obValid <- mkReg(True);
   Reg#(Bool) finCode <- mkRegU;
   Reg#(Bool) tryStatus <- mkRegU;

   Bool pageBusy = txActive && txIsData && pg(txq) == pg(wr);
   Bool rspReady = openPending || (inWin(wr, dack) && !pageBusy);

   Bit#(9) ent = lenTbl.sub(pg(txq));
   Bool haveData = txq != wr;
   Bool useData = haveData && !tryStatus;
   Bit#(8) vC = useData ? ent[7:0] : 12;
   // Credit check pipelined for timing: nextR = cursor + vC (stage 1), then okR, the verdict for
   // the selected frame kind, from d = grant - nextR (stage 2). They equal the direct tests when
   // no input was written in the last two clocks; `stale` blocks the start decision for two
   // clocks after a write to grant, cursor, txq, wr or lenTbl, a session reset, or a change of
   // tryStatus. A start therefore waits for two clocks without such a write: a few clocks in
   // the common case, about 2W clocks at most while responses complete back to back (16 at
   // W = 8, less than one 83-clock byte time at 12 Mbaud and 100 MHz). A start reuses nextR.
   Reg#(Bit#(16)) nextR <- mkRegU;
   Reg#(Bool) okR <- mkRegU;
   Reg#(Bool) stale1 <- mkReg(True);
   Reg#(Bool) stale2 <- mkReg(True);
   Bool stale = stale1 || stale2;
   // tryStatus changes (updState priority: clear before set).
   Bool modeChange = tryClrW ? tryStatus : (trySetW && !tryStatus);

   (* fire_when_enabled, no_implicit_conditions *)
   rule creditStage;
      nextR <= cursor + zeroExtend(vC);
      Bit#(16) d = grant - nextR;
      okR <= d[15] == 0 || (!useData && d[15:6] == '1);   // status: at least -64, the reserve
      stale1 <= grantWrW || isValid(cursorW.wget) || resetW || txLoadW || txEndW || lenWrW
                || wrIncW || modeChange;
      stale2 <= stale1;
   endrule
   Bool behind = (txq - dack) > (hi - dack);
   Bool quiescent = rdState == RdIdle && !wActive && cons == wr;

   (* fire_when_enabled, no_implicit_conditions *)
   rule txRam;
      rspRam.b.put(False, {pg(txq), off(t)}, ?);
   endrule

   function Action start(Bool isD) = action
      txActive <= True; txIsData <= isD; cursorW.wset(nextR); vlen <= vC; t <= 6; phase <= THdr;
      tcntT <= 0; crcT <= '1; code <= 1; finCode <= False; txClrOweW.send;
   endaction;

   (* fire_when_enabled *)
   rule txBoundary (!txActive);
      if (openPending) begin
         if (quiescent) begin
            // An rspLimit above maxLen means maxLen.
            // updState clears cursor, tryStatus, exp, dack, cons, wr, txq and hi (resetW).
            sid <= pendSid; rspLimit <= min(pendLimit, maxLen); bound <= True;
            sessGen <= !sessGen;
            rwDone <= rwHost; resetW.send; txSetOweW.send;
         end
      end
      else if (rwHost != rwDone || behind) begin
         txLoadW.send; rwDone <= rwHost;
      end
      else if (!stale) begin
         if (useData) begin
            if (okR) start(True);
            else if (owe) trySetW.send;
         end
         else begin
            tryClrW.send;
            if (owe && (okR || permit)) begin start(False); txStatusW.send; end
         end
      end
   endrule

   // Every TX byte is assembled one bit per clock into `tb` (LSB first) while the same bit goes
   // through the bit-serial CRC. Header bits come from 1-bit selections of the header fields,
   // body bits from the page RAM byte, trailer bits from the CRC register XOR the session ID.
   // Fields that may change during a frame (`exp`) are sampled at the first bit of the byte.
   Bit#(1) sBitT = !bound ? 0 : sid[{tcntT, idxT}];
   Bit#(sb) e = idxT == 0 ? exp : expT;
   Bit#(8) ctlB = {2'b10, 1'b0, pack(txIsData), pack(txIsData) & ent[8], logWBits};
   Bit#(8) seqB = t[0] == 1 ? zeroExtend(txq) : zeroExtend(e);
   Bit#(8) lenB = txIsData ? vlen : maxLen;
   Bit#(1) curBit = cursor[{t[1], idxT}];
   Bit#(1) hBit = case (t[2:0])
                     3'b110: ctlB[idxT];
                     3'b111: seqB[idxT];
                     3'b000: seqB[idxT];
                     3'b001: curBit;
                     3'b010: curBit;
                     default: lenB[idxT];
                  endcase;

   (* fire_when_enabled *)
   rule txStep (txActive && !tbFull && phase != TFin);
      Bit#(1) d = case (phase)
                     THdr: hBit;
                     TBody: rspRam.b.read[idxT];
                     default: ~crcT[0] ^ sBitT;
                  endcase;
      Bit#(1) fb = phase == TTrl ? 0 : crcT[0] ^ d;
      crcT <= (crcT >> 1) ^ (fb == 1 ? crcPoly : 0);
      tb <= {d, tb[7:1]};
      if (idxT == 0) expT <= exp;
      idxT <= idxT + 1;
      if (idxT == 7) begin
         tbFull <= True;
         if (phase == TTrl) begin
            tcntT <= tcntT + 1;
            if (tcntT == 3) phase <= TFin;
         end
         else begin
            Bit#(8) tn = t + 1;
            t <= tn;
            if (tn == vlen) phase <= TTrl;
            else if (t == 11) phase <= TBody;
         end
      end
   endrule

   (* fire_when_enabled *)
   rule txEncode (txActive && tbFull && !obValid);
      Bool z = tb == 0;
      ob <= z ? code : tb;
      code <= z ? 1 : code + 1;
      obValid <= True; tbFull <= False;
   endrule

   (* fire_when_enabled *)
   rule txFin (txActive && phase == TFin && !tbFull && !obValid);
      obValid <= True;
      if (!finCode) begin ob <= code; finCode <= True; end
      else begin
         ob <= 0; txActive <= False;
         if (txIsData) txEndW.send;
      end
   endrule

   // ---------------- Shared counters: one writer each ----------------
   (* fire_when_enabled, no_implicit_conditions *)
   rule updState;
      // Written every clock as `clear ? init : (event ? next : hold)`: yosys puts the clear on
      // the reset pin and the event on the clock enable, with no clear term in the enable.
      Bool clr = resetW || inRst;
      exp <= clr ? 0 : (commitW ? exp + 1 : exp);
      dack <= clr ? 0 : fromMaybe(dack, dackW.wget);
      cons <= clr ? 0 : (consIncW ? cons + 1 : cons);
      wr <= clr ? 0 : (wrIncW ? wr + 1 : wr);
      txq <= clr ? 0 : (txLoadW ? dack : (txEndW ? txq + 1 : txq));
      hi <= clr ? 0 : ((txEndW && txq == hi) ? hi + 1 : hi);
      cursor <= clr ? 0 : fromMaybe(cursor, cursorW.wget);
      tryStatus <= clr ? False : (tryClrW ? False : (trySetW ? True : tryStatus));
      if (setOweW || txSetOweW) owe <= True;
      else if (txClrOweW) owe <= False;
      if (setOpenW) openPending <= True;
      else if (resetW) openPending <= False;
      if (resyncW) permit <= True;
      else if (txStatusW || resetW) permit <= False;
   endrule

   // ---------------- Interfaces ----------------
   interface Put rx;
      method Action put(Bit#(8) b) if (!crcRun);
         if (b == 0) begin
            rxAny <= False;
            if (rxAny) decide;
         end
         else if (!rxAny) begin
            rxAny <= True;
            cobsCode <= b; cnt <= 1; zeroPend <= b != 8'hFF;
            pos <= 6; inHdr <= True; inTr <= False; tcnt <= 0; done <= False; over <= False;
            crc <= '1; bad <= False;
            landOK <= inWin(exp, cons); rxGen <= sessGen;
         end
         else if (blockEnd) begin
            if (zeroPend) decoded(0);
            cobsCode <= b; cnt <= 1; zeroPend <= b != 8'hFF;
         end
         else begin
            decoded(b);
            cnt <= cnt + 1;
         end
      endmethod
   endinterface

   interface Get tx;
      method ActionValue#(Bit#(8)) get if (obValid);
         obValid <= False;
         return ob;
      endmethod
   endinterface

   method UInt#(8) requestPayloadLength
      if ((rdState == RdData || rdState == RdEmpty) && !rdAbandon) = unpack(rlast - 8'd11);

   interface Get request;
      method ActionValue#(LinkTok) get if ((rdState == RdData || rdState == RdEmpty) && !rdAbandon);
         Bool emp = rdState == RdEmpty;
         Bool last = emp || ro == rlast;
         rdStarted <= !last;
         if (last) begin consIncW.send; rdState <= RdIdle; roRstW.send; end   // ro <= 11
         else begin roIncW.send; rdState <= RdWait; end
         return LinkTok { data: reqRam.b.read, last: last, empty: emp };
      endmethod
   endinterface

   interface Put response;
      method Action put(LinkTok k) if (rspReady);
         Bool byteIn = !k.empty;
         Bool room = wo != rspLimit;
         Bit#(8) woN = (byteIn && room) ? wo + 1 : wo;
         Bool trunc = wTrunc || (byteIn && !room);
         // During OPEN the writer drops tokens and writes no page state: at W = 1 the page of
         // wr can be the one on the wire.
         if (byteIn && room && !openPending) rspRam.a.put(True, {pg(wr), off(wo)}, k.data);
         if (k.last) begin
            if (!openPending) begin lenTbl.upd(pg(wr), {pack(trunc), woN}); lenWrW.send; end
            wrIncW.send; wLastW.send;     // wUpd: wo <= 12; wTrunc <= False; wActive <= False
         end
         else if (byteIn) wByteW.wset(tuple2(woN, trunc));
      endmethod
   endinterface

   method Bool responseReady = rspReady;
   method UInt#(8) responseMax = unpack(rspLimit - 12);
endmodule

endpackage
