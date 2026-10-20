package ReliableLink;

import Cobs :: *;
import FIFOF :: *;
import GetPut :: *;
import RegFile :: *;
import Vector :: *;
import UartLinkTypes :: *;

export mkReliableLink;

typedef enum { SlotFree, SlotReserved, SlotPending, SlotComplete }
   SlotState deriving (Bits, Eq, FShow);

typedef struct {
   SlotState state;
   Bit#(8) seqNo;
   Bit#(16) requestLength;
   Bit#(16) maxResponse;
   Bit#(16) responseLength;
   Bool sendPending;
   Bool replay;
   Bool txPinned;
   Bool txSentEver;
   Bool creditCounted;
} SlotMeta deriving (Bits, Eq, FShow);

typedef struct {
   UInt#(3) slot;
   LinkRequestHeader header;
} RequestDescriptor deriving (Bits, Eq, FShow);

typedef struct {
   Bit#(8) value;
   Bool last;
   Bool control;
   Bool closesSession;
   Bool statusOnly;
   UInt#(3) slot;
} TransmitByte deriving (Bits, Eq, FShow);

function Bit#(16) distance16(Bit#(16) newer, Bit#(16) older);
   return newer - older;
endfunction

function Bool forward16(Bit#(16) newer, Bit#(16) older);
   Bit#(16) distance = newer - older;
   return distance != 0 && distance < 'h8000;
endfunction

function Bool deliveryAckValid(Bit#(8) ack, Bit#(8) currentAck,
                               Bit#(8) windowLimit,
                               Vector#(8, Reg#(SlotMeta)) slots);
   Bit#(8) delta = ack - currentAck;
   Bool valid = delta == 0 || (delta < 128 && delta <= windowLimit);
   Bit#(8) seen = 0;
   for (Integer j = 0; j < 8; j = j + 1) begin
      SlotMeta meta = slots[fromInteger(j)];
      Bit#(8) offset = meta.seqNo - currentAck;
      if (meta.state != SlotFree && offset < delta && offset < 8) begin
         UInt#(3) bitIndex = unpack(truncate(offset));
         Bit#(8) slotMask = 1;
         slotMask = slotMask << bitIndex;
         if (meta.state == SlotComplete && !meta.txPinned && meta.txSentEver)
            seen = seen | slotMask;
         else
            valid = False;
      end
   end
   UInt#(4) deltaSmall = unpack(truncate(delta));
   Bit#(8) required = 1;
   required = required << deltaSmall;
   required = required - 1;
   return valid && seen == required;
endfunction

function Bit#(32) satInc(Bit#(32) value);
   return value == '1 ? value : value + 1;
endfunction

function Bit#(16) wireBound16(Bit#(16) decodedBytes);
   // All callers pass a decoded frame length at or below the compiled limit
   // (240 bytes), so rCOBS needs one code byte and the delimiter.
   return decodedBytes + 2;
endfunction

(* descending_urgency = "receiveToken, startResponse, startControl, releaseResponseSlot, countBlockedControl, acceptResponseHeader, queueTxByte" *)
module mkReliableLink#(Integer compiledFrameLimit,
                       Integer compiledWindowLimit,
                       Integer slotBytes)(ReliableLinkIfc);
   if (compiledFrameLimit < 32 || compiledFrameLimit > 240)
      error("mkReliableLink: compiledFrameLimit must be in 32..240");
   if (compiledWindowLimit < 1 || compiledWindowLimit > 8)
      error("mkReliableLink: compiledWindowLimit must be in 1..8");
   if (slotBytes < 1 || slotBytes > 256)
      error("mkReliableLink: slotBytes must be in 1..256");

   CobsDecoder decoder <- mkCobsDecoder;
   RcobsEncoder encoder <- mkRcobsEncoder;
   FIFOF#(RequestDescriptor) requestQueue <- mkSizedFIFOF(8);
   FIFOF#(LinkResponseHeader) responseHeaderQueue <- mkFIFOF;
   FIFOF#(TransmitByte) transmitQueue <- mkFIFOF;

   RegFile#(Bit#(11), Bit#(8)) requestMemory <- mkRegFileFull;
   RegFile#(Bit#(11), Bit#(8)) responseMemory <- mkRegFileFull;
   RegFile#(Bit#(5), Bit#(8)) controlReceiveMemory <- mkRegFileFull;
   Vector#(8, Reg#(Bit#(8))) controlTransmitMemory <- replicateM(mkReg(0));

   SlotMeta emptySlot = SlotMeta {
      state: SlotFree, seqNo: 0, requestLength: 0, maxResponse: 0,
      responseLength: 0, sendPending: False, replay: False, txPinned: False,
      txSentEver: False,
      creditCounted: False
   };
   Vector#(8, Reg#(SlotMeta)) slots <- replicateM(mkReg(emptySlot));
   Vector#(9, Reg#(Bit#(8))) rxHeader <- replicateM(mkReg(0));
   Vector#(9, Reg#(Bit#(8))) txHeader <- replicateM(mkReg(0));

   Reg#(UInt#(9)) rxCount <- mkReg(0);
   Reg#(Bit#(32)) rxCrc <- mkReg('1);
   Reg#(Bit#(2)) rxKind <- mkReg(0);
   Reg#(Bit#(2)) rxFlags <- mkReg(0);
   Reg#(Bool) rxDiscard <- mkReg(False);
   PulseWire rxAborting <- mkPulseWire;
   Reg#(Bool) rxReservedSlot <- mkReg(False);
   Reg#(Bool) rxBorrowedSlot <- mkReg(False);
   Reg#(Bool) rxDuplicate <- mkReg(False);
   Reg#(Bool) rxBadShape <- mkReg(False);
   Reg#(UInt#(3)) rxSlot <- mkReg(0);
   Reg#(UInt#(3)) rxDuplicateSlot <- mkReg(0);
   Reg#(Bit#(16)) rxPayloadCount <- mkReg(0);
   Reg#(Bool) pendingBorrowedSlot <- mkReg(False);
   Reg#(UInt#(3)) pendingBorrowedIndex <- mkReg(0);
   Reg#(LinkRequestHeader) pendingBorrowedHeader <- mkRegU;

   Reg#(Bool) bound <- mkReg(False);
   Reg#(Bit#(32)) sessionId <- mkReg(0);
   Reg#(Bit#(8)) expectedRequest <- mkReg(0);
   Reg#(Bit#(8)) deliveryAck <- mkReg(0);
   Reg#(Bit#(16)) grantEdge <- mkReg(0);
   Reg#(Bit#(16)) bridgeCapacity <- mkReg(0);
   Reg#(Bit#(16)) txCursor <- mkReg(0);
   Reg#(Bit#(16)) selectedFrameLimit <- mkReg(fromInteger(compiledFrameLimit));
   Reg#(Bit#(8)) selectedWindowLimit <- mkReg(fromInteger(compiledWindowLimit));
   Reg#(Bit#(16)) openRequestedFrame <- mkReg(0);
   Reg#(Bit#(8)) openRequestedWindow <- mkReg(0);
   Reg#(Bit#(16)) openBudget <- mkReg(0);
   Reg#(Bit#(8)) openToken <- mkReg(0);
   Reg#(Bool) pendingApplicationAck <- mkReg(False);
   Reg#(Bit#(8)) pendingAckValue <- mkReg(0);
   Reg#(Bit#(16)) pendingGrantValue <- mkReg(0);

   Reg#(Bool) requestActive <- mkReg(False);
   Reg#(UInt#(3)) requestSlot <- mkReg(0);
   Reg#(Bit#(16)) requestReadPos <- mkReg(0);

   Reg#(Bool) responseActive <- mkReg(False);
   Reg#(UInt#(3)) responseSlot <- mkReg(0);
   Reg#(Bit#(16)) responseWritePos <- mkReg(0);

   Reg#(Bool) controlPending <- mkReg(False);
   Reg#(Bool) gapPending <- mkReg(False);
   Reg#(Bool) closeAfterControl <- mkReg(False);
   Reg#(Bool) closeCompletePending <- mkReg(False);
   Reg#(Bool) controlCreditCounted <- mkReg(False);
   Reg#(Bool) controlUsesReserve <- mkReg(False);
   Reg#(Bit#(8)) controlToken <- mkReg(0);
   Reg#(Bit#(2)) controlOpcode <- mkReg(0);
   Reg#(Bit#(16)) controlResponseLength <- mkReg(0);
   Reg#(Bit#(16)) controlFrameLimit <- mkReg(0);
   Reg#(Bit#(8)) controlWindowLimit <- mkReg(0);
   Reg#(Bit#(32)) controlSession <- mkReg(0);
   Vector#(8, Reg#(Bit#(8))) txControlMemory <- replicateM(mkReg(0));

   Reg#(Bool) txActive <- mkReg(False);
   Reg#(Bool) txIsControl <- mkReg(False);
   Reg#(Bool) txClosesSession <- mkReg(False);
   Reg#(Bool) txStatusOnly <- mkReg(False);
   Reg#(UInt#(3)) txSlot <- mkReg(0);
   Reg#(Bit#(8)) txSeq <- mkReg(0);
   Reg#(Bit#(2)) txKind <- mkReg(0);
   Reg#(Bit#(2)) txFlags <- mkReg(0);
   Reg#(Bit#(16)) txPayloadLength <- mkReg(0);
   Reg#(Bit#(16)) txFeedPos <- mkReg(0);
   Reg#(Bit#(32)) txSession <- mkReg(0);
   Reg#(Bit#(16)) txAnchor <- mkReg(0);
   Reg#(Bit#(32)) txCrc <- mkReg('1);
   Reg#(Bool) txEndFed <- mkReg(False);
   Reg#(Bool) txReleasePending <- mkReg(False);
   Reg#(UInt#(3)) txReleaseSlot <- mkReg(0);

   Reg#(Bit#(32)) statRx <- mkReg(0);
   Reg#(Bit#(32)) statTx <- mkReg(0);
   Reg#(Bit#(32)) statFraming <- mkReg(0);
   Reg#(Bit#(32)) statParity <- mkReg(0);
   Reg#(Bit#(32)) statBreak <- mkReg(0);
   Reg#(Bit#(32)) statOverrun <- mkReg(0);
   Reg#(Bit#(32)) statValidRequest <- mkReg(0);
   Reg#(Bit#(32)) statCobs <- mkReg(0);
   Reg#(Bit#(32)) statCrc <- mkReg(0);
   Reg#(Bit#(32)) statLength <- mkReg(0);
   Reg#(Bit#(32)) statHeader <- mkReg(0);
   Reg#(Bit#(32)) statGap <- mkReg(0);
   Reg#(Bit#(32)) statDuplicate <- mkReg(0);
   Reg#(Bit#(32)) statDrops <- mkReg(0);
   Reg#(Bit#(32)) statResponseTx <- mkReg(0);
   Reg#(Bit#(32)) statCreditBlocked <- mkReg(0);

   function Bit#(11) memoryAddress(UInt#(3) slot, Bit#(8) offset);
      return {pack(slot), offset};
   endfunction

   function Bit#(32) headerSession();
      return {rxHeader[8], rxHeader[7], rxHeader[6], rxHeader[5]};
   endfunction

   function Bit#(16) headerCursor();
      return {rxHeader[4], rxHeader[3]};
   endfunction

   function Bit#(16) headerPayloadLength();
      return zeroExtend(pack(rxCount)) - 13;
   endfunction

   function Bit#(16) boundedFrame(Bit#(16) budget);
      Bit#(16) result = 0;
      for (Integer n = 32; n <= 240; n = n + 1)
         if (n <= compiledFrameLimit &&
             fromInteger(maxWireBytes(n)) + 45 <= budget)
            result = fromInteger(n);
      return result;
   endfunction

   function Bool gapCreditAvailable;
      return (grantEdge - txCursor) >= fromInteger(maxWireBytes(13) + 45);
   endfunction

   function Bit#(16) controlWireBound;
      if (controlResponseLength == 0)
         return fromInteger(maxWireBytes(13));
      else
         return fromInteger(maxWireBytes(13 + 4));
   endfunction

   function Bool controlSendable;
      Bit#(16) wireBound = controlWireBound;
      Bit#(16) remaining = grantEdge - txCursor;
         Bool reserveOk = controlUsesReserve || remaining >= wireBound + 45;
      return bound && controlSession == sessionId && reserveOk &&
         remaining >= wireBound;
   endfunction

   function Bool hasSendableResponse;
      Bool found = False;
      Bit#(16) remaining = grantEdge - txCursor;
      UInt#(8) windowLimit = unpack(selectedWindowLimit);
      for (Integer i = 0; i < 8; i = i + 1) begin
         SlotMeta meta = slots[fromInteger(i)];
         Bit#(16) wireBound = wireBound16(13 + meta.responseLength);
         if (fromInteger(i) < windowLimit &&
             meta.state == SlotComplete &&
             meta.sendPending && !meta.txPinned &&
             remaining >= wireBound + 45)
            found = True;
      end
      return found;
   endfunction

   function Bit#(8) frameByte(Bit#(16) index);
      case (index)
         0: return txHeader[0];
         1: return txHeader[1];
         2: return txHeader[2];
         3: return txHeader[3];
         4: return txHeader[4];
         5: return txHeader[5];
         6: return txHeader[6];
         7: return txHeader[7];
         8: return txHeader[8];
         default: begin
            Bit#(16) payloadIndex = index - 9;
            if (txIsControl) begin
               UInt#(3) controlIndex = unpack(truncate(payloadIndex));
               return txControlMemory[controlIndex];
            end
            else
               return responseMemory.sub(memoryAddress(txSlot, truncate(payloadIndex)));
         end
      endcase
   endfunction

   rule receiveToken (!rxAborting && !txReleasePending &&
                      !closeAfterControl &&
                      !closeCompletePending &&
                      !(gapPending && gapCreditAvailable && !controlPending) &&
                      !pendingApplicationAck);
      let token <- decoder.out.get;
      case (token) matches
         tagged Byte .value: begin
            if (rxDiscard) begin
               if (rxCount != unpack('1)) rxCount <= rxCount + 1;
            end
            else if (zeroExtend(pack(rxCount)) >= selectedFrameLimit) begin
               rxDiscard <= True;
               statLength <= satInc(statLength);
               if (rxReservedSlot && !rxBorrowedSlot)
                  slots[rxSlot] <= emptySlot;
               rxReservedSlot <= False;
               rxBorrowedSlot <= False;
            end
            else begin
               if (rxCount < 9) begin
                  UInt#(4) headerIndex = truncate(rxCount);
                  rxHeader[headerIndex] <= value;
                  if (rxCount == 0) begin
                     rxKind <= value[5:4];
                     rxFlags <= value[1:0];
                     rxCount <= 1;
                     rxCrc <= crc32cUpdate(rxCrc, value);
                  end
                  else begin
                     if (rxCount == 1 && rxKind == 0) begin
                        Bool found = False;
                        Bool duplicate = False;
                        Bool borrowFound = False;
                        UInt#(3) picked = 0;
                        UInt#(3) borrowed = 0;
                        UInt#(8) windowLimit = unpack(selectedWindowLimit);
                        for (Integer i = 0; i < 8; i = i + 1) begin
                           SlotMeta meta = slots[fromInteger(i)];
                           if (meta.state != SlotFree && meta.seqNo == value) begin
                              duplicate = True;
                              picked = fromInteger(i);
                           end
                           if (!duplicate && !found && meta.state == SlotFree &&
                               fromInteger(i) < windowLimit) begin
                              found = True;
                              picked = fromInteger(i);
                           end
                           if (!borrowFound && meta.state == SlotComplete &&
                               meta.seqNo == deliveryAck && !meta.txPinned &&
                               fromInteger(i) < windowLimit) begin
                              borrowFound = True;
                              borrowed = fromInteger(i);
                           end
                        end
                        if (duplicate) begin
                           rxDuplicate <= True;
                           rxDuplicateSlot <= picked;
                        end
                        else if (value == expectedRequest) begin
                           if (found) begin
                              SlotMeta meta = emptySlot;
                              meta.state = SlotReserved;
                              meta.seqNo = value;
                              meta.maxResponse = selectedFrameLimit - 13;
                              slots[picked] <= meta;
                              rxReservedSlot <= True;
                              rxBorrowedSlot <= False;
                              rxSlot <= picked;
                           end
                           else if (borrowFound) begin
                              rxReservedSlot <= True;
                              rxBorrowedSlot <= True;
                              rxSlot <= borrowed;
                           end
                        end
                     end
                     rxCount <= rxCount + 1;
                     rxCrc <= crc32cUpdate(rxCrc, value);
                  end
               end
               else begin
                  if (rxKind == 2 && rxCount < 41)
                     controlReceiveMemory.upd(truncate(pack(rxCount) - 9), value);
                  else if (rxKind == 0 && rxReservedSlot && rxCount < 265)
                     requestMemory.upd(memoryAddress(rxSlot, truncate(pack(rxCount) - 9)), value);
                  rxPayloadCount <= rxPayloadCount + 1;
                  rxCount <= rxCount + 1;
                  rxCrc <= crc32cUpdate(rxCrc, value);
               end
            end
         end
         tagged End .valid: begin
            if (rxDiscard) begin
               rxDiscard <= False;
            end
            else if (!valid) begin
               statCobs <= satInc(statCobs);
               if (rxReservedSlot && !rxBorrowedSlot) slots[rxSlot] <= emptySlot;
            end
            else if (rxCount < 13 || zeroExtend(pack(rxCount)) > selectedFrameLimit ||
                     rxCrc != 32'hB798B438) begin
               if (rxCrc != 32'hB798B438) statCrc <= satInc(statCrc);
               else statLength <= satInc(statLength);
               if (rxReservedSlot && !rxBorrowedSlot) slots[rxSlot] <= emptySlot;
            end
            else begin
               Bit#(8) ctl = rxHeader[0];
               Bit#(8) seqNo = rxHeader[1];
               Bit#(8) ack = rxHeader[2];
               Bit#(16) cursor = headerCursor;
               Bit#(32) frameSession = headerSession;
               Bit#(16) payloadLength = headerPayloadLength;
               if (ctl[7:6] != 1 || ctl[3:2] != 0) begin
                  statHeader <= satInc(statHeader);
                  if (rxReservedSlot && !rxBorrowedSlot) slots[rxSlot] <= emptySlot;
               end
               else if (rxKind == 0 && rxFlags != 0) begin
                  statHeader <= satInc(statHeader);
                  if (rxReservedSlot && !rxBorrowedSlot) slots[rxSlot] <= emptySlot;
               end
               else if (rxKind == 2) begin
                  Bit#(2) opcode = ctl[1:0];
                  Bool isOpen = opcode == 2;
                  Bool isPoll = opcode == 0;
                  Bool isCaps = opcode == 1;
                  Bool isClose = opcode == 3;
                  if ((isOpen && payloadLength == 4 && frameSession != 0 &&
                       controlReceiveMemory.sub(3) == 0) &&
                      (!bound || frameSession == sessionId)) begin
                     Bit#(16) requestedFrame = {controlReceiveMemory.sub(1),
                                                 controlReceiveMemory.sub(0)};
                     Bit#(8) requestedWindow = controlReceiveMemory.sub(2);
                     Bit#(16) budget = cursor;
                     if (!bound && budget >= 80 && budget < 32768 &&
                         requestedFrame >= 32 && requestedWindow != 0) begin
                        Bit#(16) chosen = boundedFrame(budget);
                        if (chosen >= 32) begin
                           bound <= True;
                           sessionId <= frameSession;
                           expectedRequest <= 0;
                           deliveryAck <= 0;
                           txCursor <= 0;
                           grantEdge <= budget;
                           bridgeCapacity <= budget;
                           selectedFrameLimit <= requestedFrame < chosen ? requestedFrame : chosen;
                           selectedWindowLimit <= requestedWindow < fromInteger(compiledWindowLimit) ?
                              requestedWindow : fromInteger(compiledWindowLimit);
                           controlPending <= True;
                           controlUsesReserve <= True;
                           gapPending <= False;
                           controlOpcode <= opcode;
                           controlToken <= seqNo;
                           controlSession <= frameSession;
                           controlFrameLimit <= requestedFrame < chosen ? requestedFrame : chosen;
                           controlWindowLimit <= requestedWindow < fromInteger(compiledWindowLimit) ?
                              requestedWindow : fromInteger(compiledWindowLimit);
                           openRequestedFrame <= requestedFrame;
                           openRequestedWindow <= requestedWindow;
                           openBudget <= budget;
                           openToken <= seqNo;
                           controlResponseLength <= 4;
                           controlTransmitMemory[0] <= truncate(requestedFrame < chosen ?
                              requestedFrame : chosen);
                           controlTransmitMemory[1] <= truncate((requestedFrame < chosen ?
                              requestedFrame : chosen) >> 8);
                           controlTransmitMemory[2] <= requestedWindow < fromInteger(compiledWindowLimit) ?
                              requestedWindow : fromInteger(compiledWindowLimit);
                           controlTransmitMemory[3] <= 0;
                        end
                     end
                     else if (bound && seqNo == openToken && budget == openBudget &&
                              requestedFrame == openRequestedFrame &&
                              requestedWindow == openRequestedWindow) begin
                        controlPending <= True;
                        controlUsesReserve <= True;
                        controlOpcode <= opcode;
                        controlToken <= seqNo;
                        controlSession <= frameSession;
                        controlFrameLimit <= selectedFrameLimit;
                        controlWindowLimit <= selectedWindowLimit;
                        controlResponseLength <= 4;
                        controlTransmitMemory[0] <= truncate(selectedFrameLimit);
                        controlTransmitMemory[1] <= truncate(selectedFrameLimit >> 8);
                        controlTransmitMemory[2] <= selectedWindowLimit;
                        controlTransmitMemory[3] <= 0;
                     end
                  end
                  else if (!deliveryAckValid(ack, deliveryAck,
                                             selectedWindowLimit, slots)) begin
                     statHeader <= satInc(statHeader);
                  end
                  else if (bound && frameSession == sessionId &&
                           ((isPoll || isCaps || isClose) && payloadLength == 0)) begin
                     if (forward16(cursor, grantEdge) &&
                         distance16(cursor, txCursor) <= bridgeCapacity &&
                         distance16(cursor, txCursor) < 32768)
                        grantEdge <= cursor;
                     Bool ackAccepted = False;
                     if (ack != deliveryAck) begin
                        // deliveryAckValid has already checked every sequence and slot.
                        ackAccepted = True;
                        for (Integer j = 0; j < 8; j = j + 1) begin
                           SlotMeta meta = slots[fromInteger(j)];
                           Bit#(8) amount = ack - meta.seqNo;
                           if (meta.state != SlotFree && amount != 0 && amount < 128 &&
                               !meta.txPinned)
                              slots[fromInteger(j)] <= emptySlot;
                        end
                        deliveryAck <= ack;
                     end
                     Bool willBeEmpty = True;
                     for (Integer i = 0; i < 8; i = i + 1) begin
                        SlotMeta meta = slots[fromInteger(i)];
                        Bit#(8) amount = ack - meta.seqNo;
                        Bool willRetire = ackAccepted &&
                           meta.state != SlotFree && amount != 0 &&
                           amount < 128 && !meta.txPinned;
                        if (meta.state != SlotFree && !willRetire)
                           willBeEmpty = False;
                     end
                     if (!controlPending) begin
                        if (isClose && !willBeEmpty) begin
                           statHeader <= satInc(statHeader);
                        end
                        else begin
                           controlPending <= True;
                           controlUsesReserve <= isPoll || isClose;
                           controlOpcode <= opcode;
                           controlToken <= seqNo;
                           controlSession <= frameSession;
                           controlFrameLimit <= selectedFrameLimit;
                           controlWindowLimit <= selectedWindowLimit;
                           controlResponseLength <= isCaps ? 4 :
                              (isOpen ? 4 : 0);
                           if (isClose) closeAfterControl <= True;
                           if (isCaps) begin
                              controlTransmitMemory[0] <= fromInteger(compiledFrameLimit);
                              controlTransmitMemory[1] <= 0;
                              controlTransmitMemory[2] <= fromInteger(compiledWindowLimit);
                              controlTransmitMemory[3] <= 1;
                           end
                           else if (isOpen) begin
                              controlTransmitMemory[0] <= truncate(selectedFrameLimit);
                              controlTransmitMemory[1] <= truncate(selectedFrameLimit >> 8);
                              controlTransmitMemory[2] <= selectedWindowLimit;
                              controlTransmitMemory[3] <= 1;
                           end
                        end
                     end
                  end
                  else statHeader <= satInc(statHeader);
               end
               else if (rxKind == 0 && bound && frameSession == sessionId &&
                        !deliveryAckValid(ack, deliveryAck,
                                          selectedWindowLimit, slots)) begin
                  statHeader <= satInc(statHeader);
                  if (rxReservedSlot && !rxBorrowedSlot) slots[rxSlot] <= emptySlot;
               end
               else if (rxKind == 0 && bound && frameSession == sessionId &&
                        rxFlags == 0 && payloadLength <= fromInteger(slotBytes) &&
                        rxBorrowedSlot && ack == deliveryAck &&
                        seqNo == expectedRequest) begin
                  pendingApplicationAck <= True;
                  pendingAckValue <= ack;
                  pendingGrantValue <= cursor;
                  statDrops <= satInc(statDrops);
               end
               else if (rxKind == 0 && bound && frameSession == sessionId &&
                        rxFlags == 0 && payloadLength <= fromInteger(slotBytes) &&
                        rxReservedSlot && seqNo == expectedRequest) begin
                  Bit#(16) frameResponseLimit = selectedFrameLimit - 13;
                  Bit#(16) slotResponseLimit = fromInteger(slotBytes);
                  Bit#(16) responseLimit = frameResponseLimit < slotResponseLimit ?
                                            frameResponseLimit : slotResponseLimit;
                  pendingApplicationAck <= True;
                  pendingAckValue <= ack;
                  pendingGrantValue <= cursor;
                  LinkRequestHeader request = LinkRequestHeader {
                     seqNo: seqNo, length: payloadLength,
                     maxResponse: responseLimit
                  };
                  if (rxBorrowedSlot) begin
                     pendingBorrowedSlot <= True;
                     pendingBorrowedIndex <= rxSlot;
                     pendingBorrowedHeader <= request;
                  end
                  else begin
                     SlotMeta meta = slots[rxSlot];
                     meta.state = SlotPending;
                     meta.requestLength = payloadLength;
                     meta.maxResponse = responseLimit;
                     slots[rxSlot] <= meta;
                     requestQueue.enq(RequestDescriptor { slot: rxSlot, header: request });
                     expectedRequest <= expectedRequest + 1;
                     statValidRequest <= satInc(statValidRequest);
                  end
               end
               else if (rxKind == 0 && bound && frameSession == sessionId &&
                        rxFlags == 0 && payloadLength <= fromInteger(slotBytes) &&
                        !rxReservedSlot && seqNo == expectedRequest) begin
                  pendingApplicationAck <= True;
                  pendingAckValue <= ack;
                  pendingGrantValue <= cursor;
                  statDrops <= satInc(statDrops);
               end
               else if (rxKind == 0 && bound && frameSession == sessionId &&
                        rxDuplicate) begin
                  pendingApplicationAck <= True;
                  pendingAckValue <= ack;
                  pendingGrantValue <= cursor;
                  SlotMeta meta = slots[rxDuplicateSlot];
                  statDuplicate <= satInc(statDuplicate);
                  if (meta.state == SlotComplete && !meta.txPinned) begin
                     meta.sendPending = True;
                     meta.replay = True;
                     meta.creditCounted = False;
                     slots[rxDuplicateSlot] <= meta;
                  end
               end
               else if (rxKind == 0 && bound && frameSession == sessionId &&
                        seqNo != expectedRequest) begin
                  pendingApplicationAck <= True;
                  pendingAckValue <= ack;
                  pendingGrantValue <= cursor;
                  statGap <= satInc(statGap);
                  Bit#(8) delta = seqNo - expectedRequest;
                  if (delta != 0 && delta < 128) gapPending <= True;
               end
               else begin
                  statHeader <= satInc(statHeader);
                  if (rxReservedSlot && !rxBorrowedSlot) slots[rxSlot] <= emptySlot;
               end
            end
            rxCount <= 0;
            rxCrc <= '1;
            rxPayloadCount <= 0;
            rxReservedSlot <= False;
            rxBorrowedSlot <= False;
            rxDuplicate <= False;
            rxBadShape <= False;
         end
      endcase
   endrule

   rule applyPendingApplicationAck (pendingApplicationAck && !rxAborting);
      Bit#(16) cursor = pendingGrantValue;
      Bit#(8) ack = pendingAckValue;
      if (deliveryAckValid(ack, deliveryAck, selectedWindowLimit, slots)) begin
         if (forward16(cursor, grantEdge) &&
             distance16(cursor, txCursor) <= bridgeCapacity &&
             distance16(cursor, txCursor) < 32768)
            grantEdge <= cursor;
         if (ack != deliveryAck) begin
            for (Integer j = 0; j < 8; j = j + 1) begin
               SlotMeta meta = slots[fromInteger(j)];
               Bit#(8) amount = ack - meta.seqNo;
               if (meta.state != SlotFree && amount != 0 && amount < 128 &&
                   !meta.txPinned) begin
                  if (pendingBorrowedSlot &&
                      fromInteger(j) == pendingBorrowedIndex) begin
                     meta = emptySlot;
                     meta.state = SlotPending;
                     meta.seqNo = pendingBorrowedHeader.seqNo;
                     meta.requestLength = pendingBorrowedHeader.length;
                     meta.maxResponse = pendingBorrowedHeader.maxResponse;
                  end
                  else
                     meta = emptySlot;
                  slots[fromInteger(j)] <= meta;
               end
            end
            deliveryAck <= ack;
            if (pendingBorrowedSlot) begin
               requestQueue.enq(RequestDescriptor {
                  slot: pendingBorrowedIndex, header: pendingBorrowedHeader
               });
               expectedRequest <= expectedRequest + 1;
               statValidRequest <= satInc(statValidRequest);
            end
         end
      end
      else begin
         statHeader <= satInc(statHeader);
      end
      pendingApplicationAck <= False;
      pendingBorrowedSlot <= False;
   endrule

   rule applyAbort (rxAborting);
      rxCount <= 0;
      rxCrc <= '1;
      rxPayloadCount <= 0;
      rxReservedSlot <= False;
      rxBorrowedSlot <= False;
      rxDuplicate <= False;
      rxDiscard <= False;
      if (rxReservedSlot && !rxBorrowedSlot) slots[rxSlot] <= emptySlot;
   endrule

   rule feedTx (txActive && !txEndFed && !transmitQueue.notEmpty);
      Bit#(16) rawLength = 9 + txPayloadLength;
      if (txFeedPos < rawLength) begin
         Bit#(8) value = frameByte(txFeedPos);
         encoder.in.put(tagged Byte value);
         if (txFeedPos < rawLength) txCrc <= crc32cUpdate(txCrc, value);
         txFeedPos <= txFeedPos + 1;
      end
      else if (txFeedPos < rawLength + 4) begin
         Bit#(32) finalCrc = ~txCrc;
         Bit#(2) byteIndex = truncate(txFeedPos - rawLength);
         Bit#(8) crcByte = case (byteIndex)
            0: finalCrc[7:0];
            1: finalCrc[15:8];
            2: finalCrc[23:16];
            default: finalCrc[31:24];
         endcase;
         encoder.in.put(tagged Byte crcByte);
         txFeedPos <= txFeedPos + 1;
      end
      else begin
         encoder.in.put(tagged End True);
         txEndFed <= True;
      end
   endrule

   rule queueTxByte (txActive);
      let value <- encoder.out.get;
      transmitQueue.enq(TransmitByte {
         value: value, last: value == 0, control: txIsControl,
         closesSession: txClosesSession, statusOnly: txStatusOnly, slot: txSlot
      });
      txCursor <= txCursor + 1;
      statTx <= satInc(statTx);
   endrule

   rule finishClose (closeCompletePending && !transmitQueue.notEmpty);
      bound <= False;
      sessionId <= 0;
      closeAfterControl <= False;
      closeCompletePending <= False;
      gapPending <= False;
   endrule

   rule releaseResponseSlot (txReleasePending && !rxAborting &&
                             !pendingApplicationAck);
      SlotMeta meta = slots[txReleaseSlot];
      meta.txPinned = False;
      meta.txSentEver = True;
      slots[txReleaseSlot] <= meta;
      txReleasePending <= False;
   endrule

   rule startControl (controlPending && controlSendable && !txActive &&
                       !transmitQueue.notEmpty && !pendingApplicationAck);
         txActive <= True;
         txIsControl <= True;
         txClosesSession <= closeAfterControl;
         txStatusOnly <= False;
         txKind <= 3;
         txFlags <= 0;
         txSeq <= controlToken;
         txPayloadLength <= controlResponseLength;
         txFeedPos <= 0;
         txCrc <= '1;
         txEndFed <= False;
         txAnchor <= txCursor;
         txSession <= sessionId;
         txHeader[0] <= ctlVersion | ctlControlResponse | zeroExtend(controlOpcode);
         txHeader[1] <= controlToken;
         txHeader[2] <= expectedRequest;
         txHeader[3] <= truncate(txCursor);
         txHeader[4] <= truncate(txCursor >> 8);
         txHeader[5] <= truncate(sessionId);
         txHeader[6] <= truncate(sessionId >> 8);
         txHeader[7] <= truncate(sessionId >> 16);
         txHeader[8] <= truncate(sessionId >> 24);
         for (Integer i = 0; i < 8; i = i + 1)
            txControlMemory[fromInteger(i)] <= controlTransmitMemory[fromInteger(i)];
         controlPending <= False;
         controlCreditCounted <= False;
   endrule

   rule countBlockedControl (controlPending && bound &&
                             controlSession == sessionId &&
                             !controlSendable && !controlCreditCounted);
      statCreditBlocked <= satInc(statCreditBlocked);
      controlCreditCounted <= True;
   endrule

   rule startGapResponse (gapPending && gapCreditAvailable &&
                          !controlPending && !txActive && bound &&
                          !rxAborting && !pendingApplicationAck &&
                          !closeAfterControl && !closeCompletePending &&
                          !txReleasePending && !transmitQueue.notEmpty);
      begin
         txActive <= True;
         txIsControl <= False;
         txClosesSession <= False;
         txStatusOnly <= True;
         txKind <= 1;
         txFlags <= flagGap;
         txSeq <= expectedRequest;
         txPayloadLength <= 0;
         txFeedPos <= 0;
         txCrc <= '1;
         txEndFed <= False;
         txAnchor <= txCursor;
         txSession <= sessionId;
         txHeader[0] <= ctlVersion | ctlResponse | zeroExtend(flagGap);
         txHeader[1] <= expectedRequest;
         txHeader[2] <= expectedRequest;
         txHeader[3] <= truncate(txCursor);
         txHeader[4] <= truncate(txCursor >> 8);
         txHeader[5] <= truncate(sessionId);
         txHeader[6] <= truncate(sessionId >> 8);
         txHeader[7] <= truncate(sessionId >> 16);
         txHeader[8] <= truncate(sessionId >> 24);
         gapPending <= False;
      end
   endrule

   rule startResponse (hasSendableResponse && !gapPending && !controlPending && !txActive && bound && !rxAborting &&
                       !pendingApplicationAck &&
                       !responseActive && !txReleasePending && !transmitQueue.notEmpty &&
                       True);
      Bool found = False;
      UInt#(3) picked = 0;
      SlotMeta pickedMeta = emptySlot;
      Bit#(16) remainingCredit = grantEdge - txCursor;
      UInt#(8) windowLimit = unpack(selectedWindowLimit);
      for (Integer i = 0; i < 8; i = i + 1) begin
         SlotMeta meta = slots[fromInteger(i)];
         Bit#(16) candidateBound = wireBound16(13 + meta.responseLength);
         Bool candidate = meta.state == SlotComplete && meta.sendPending &&
            !meta.txPinned &&
            fromInteger(i) < windowLimit &&
            remainingCredit >= candidateBound + 45;
         if (!found && candidate) begin
            found = True;
            picked = fromInteger(i);
            pickedMeta = meta;
         end
      end
      if (found) begin
         Bit#(16) wireBound = wireBound16(13 + pickedMeta.responseLength);
         Bit#(16) remaining = grantEdge - txCursor;
         pickedMeta.sendPending = False;
            pickedMeta.txPinned = True;
            pickedMeta.creditCounted = False;
            txActive <= True;
            txIsControl <= False;
            txClosesSession <= False;
            txStatusOnly <= False;
            txSlot <= picked;
            txKind <= 1;
            txFlags <= pickedMeta.replay ? flagReplay : 0;
            txSeq <= pickedMeta.seqNo;
            txPayloadLength <= pickedMeta.responseLength;
            txFeedPos <= 0;
            txCrc <= '1;
            txEndFed <= False;
            txAnchor <= txCursor;
            txSession <= sessionId;
            txHeader[0] <= ctlVersion | ctlResponse |
               zeroExtend(pickedMeta.replay ? flagReplay : 0);
            txHeader[1] <= pickedMeta.seqNo;
            txHeader[2] <= expectedRequest;
            txHeader[3] <= truncate(txCursor);
            txHeader[4] <= truncate(txCursor >> 8);
            txHeader[5] <= truncate(sessionId);
            txHeader[6] <= truncate(sessionId >> 8);
            txHeader[7] <= truncate(sessionId >> 16);
            txHeader[8] <= truncate(sessionId >> 24);
         pickedMeta.replay = False;
         slots[picked] <= pickedMeta;
      end
   endrule

   rule acceptResponseHeader (responseHeaderQueue.notEmpty && !responseActive &&
                              !rxAborting && !txReleasePending &&
                              !pendingApplicationAck &&
                              !transmitQueue.notEmpty);
      let header = responseHeaderQueue.first;
      responseHeaderQueue.deq;
      Bool found = False;
      UInt#(3) picked = 0;
      for (Integer i = 0; i < 8; i = i + 1) begin
         SlotMeta meta = slots[fromInteger(i)];
         if (!found && meta.state == SlotPending && meta.seqNo == header.seqNo) begin
            found = True;
            picked = fromInteger(i);
         end
      end
      if (found && header.length <= fromInteger(slotBytes) &&
          header.length <= slots[picked].maxResponse) begin
         Bool emptyResponse = header.length == 0;
         responseActive <= !emptyResponse;
         responseSlot <= picked;
         responseWritePos <= 0;
         SlotMeta meta = slots[picked];
         meta.responseLength = header.length;
         if (emptyResponse) begin
            meta.state = SlotComplete;
            meta.sendPending = True;
            meta.creditCounted = False;
         end
         slots[picked] <= meta;
      end
   endrule

   interface rx = interface Put;
      method Action put(Bit#(8) value);
         decoder.in.put(value);
         statRx <= satInc(statRx);
      endmethod
   endinterface;

   interface tx = interface Get;
      method ActionValue#(Bit#(8)) get if (!rxAborting && !txReleasePending);
         let item = transmitQueue.first;
         transmitQueue.deq;
         if (item.last) begin
            txActive <= False;
            txEndFed <= False;
            if (item.closesSession) closeCompletePending <= True;
            if (!item.control && !item.statusOnly) begin
               txReleasePending <= True;
               txReleaseSlot <= item.slot;
               statResponseTx <= satInc(statResponseTx);
            end
         end
         return item.value;
      endmethod
   endinterface;

   interface requestHeader = interface Get;
      method ActionValue#(LinkRequestHeader) get if (requestQueue.notEmpty &&
         !requestActive && !pendingApplicationAck);
         let descriptor = requestQueue.first;
         requestQueue.deq;
         requestActive <= descriptor.header.length != 0;
         requestSlot <= descriptor.slot;
         requestReadPos <= 0;
         return descriptor.header;
      endmethod
   endinterface;

   interface requestPayload = interface Get;
      method ActionValue#(Bit#(8)) get if (requestActive &&
         requestReadPos < slots[requestSlot].requestLength);
         Bit#(8) value = requestMemory.sub(memoryAddress(requestSlot,
                                                         truncate(requestReadPos)));
         requestReadPos <= requestReadPos + 1;
         if (requestReadPos + 1 == slots[requestSlot].requestLength)
            requestActive <= False;
         return value;
      endmethod
   endinterface;

   interface responseHeader = toPut(responseHeaderQueue);

   interface responsePayload = interface Put;
      method Action put(Bit#(8) value) if (responseActive && !rxAborting &&
                                           !txReleasePending &&
                                           !pendingApplicationAck &&
                                           !controlPending &&
                                           !transmitQueue.notEmpty &&
                                           !decoder.outputReady);
         responseMemory.upd(memoryAddress(responseSlot, truncate(responseWritePos)), value);
         responseWritePos <= responseWritePos + 1;
         if (responseWritePos + 1 == slots[responseSlot].responseLength) begin
            SlotMeta meta = slots[responseSlot];
            meta.state = SlotComplete;
            meta.sendPending = True;
            meta.creditCounted = False;
            if (grantEdge - txCursor <
                wireBound16(13 + meta.responseLength) + 45) begin
               meta.creditCounted = True;
               statCreditBlocked <= satInc(statCreditBlocked);
            end
            slots[responseSlot] <= meta;
            responseActive <= False;
         end
      endmethod
   endinterface;

   method Action abortRx;
      decoder.abort;
      rxAborting.send;
   endmethod

   method Action recordPhyFault(Bit#(2) kind);
      case (kind)
         0: statFraming <= satInc(statFraming);
         1: statParity <= satInc(statParity);
         2: statBreak <= satInc(statBreak);
         3: statOverrun <= satInc(statOverrun);
      endcase
   endmethod

   method Bit#(32) stat(Bit#(4) index);
      case (index)
         0: return statRx;
         1: return statTx;
         2: return statFraming;
         3: return statParity;
         4: return statBreak;
         5: return statOverrun;
         6: return statValidRequest;
         7: return statCobs;
         8: return statCrc;
         9: return statLength;
         10: return statHeader;
         11: return statGap;
         12: return statDuplicate;
         13: return statDrops;
         14: return statResponseTx;
         default: return statCreditBlocked;
      endcase
   endmethod
endmodule

endpackage
