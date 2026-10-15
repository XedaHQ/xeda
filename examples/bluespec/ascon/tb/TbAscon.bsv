// Self-checking testbench for the standalone Ascon v1.2 core.
package TbAscon;

import Vector::*;
import GetPut::*;
import AsconTypes::*;
import Ascon::*;
import AsconVectors::*;

`ifdef ASCON128A
Bool buildVariantA = True;
`else
Bool buildVariantA = False;
`endif

typedef enum { WaitCase, LoadKey, SendNonce, SendAD, SendData, ReadOutput }
   TestPhase deriving (Bits, Eq, FShow);

function Bit#(32) wordAt(Bit#(128) value, UInt#(2) index);
   case (index)
      0: return value[31:0];
      1: return value[63:32];
      2: return value[95:64];
      default: return value[127:96];
   endcase
endfunction

function Bit#(8) byteAt128(Bit#(128) value, UInt#(8) position);
   Vector#(16, Bit#(8)) bytes = unpack(value);
   UInt#(4) index = truncate(position);
   return bytes[index];
endfunction

function Bit#(32) packFour(Bit#(8) b0, Bit#(8) b1, Bit#(8) b2, Bit#(8) b3);
   return {b3, b2, b1, b0};
endfunction

function Bit#(3) wordBytes(UInt#(8) length, UInt#(8) position);
   UInt#(8) remaining = length - position;
   return pack(remaining) >= 8'd4 ? 3'd4 : truncate(pack(remaining));
endfunction

function Bit#(8) adByte(AsconTest test, UInt#(8) position);
   UInt#(6) index = truncate(position);
   return test.ad[index];
endfunction

function Bit#(8) inputByte(AsconTest test, UInt#(8) position);
   UInt#(8) index = truncate(position);
   return test.inputData[index];
endfunction

function Bit#(8) expectedByte(AsconTest test, UInt#(8) position);
   UInt#(8) index = truncate(position);
   return test.expected[index];
endfunction

function Bit#(4) keepFor(Bit#(3) count);
   case (count)
      0: return 4'b0000;
      1: return 4'b0001;
      2: return 4'b0011;
      3: return 4'b0111;
      default: return 4'b1111;
   endcase
endfunction

(* synthesize, descending_urgency = "readOutput, sendData" *)
module mkTbAscon(Empty);
   AsconIfc dut <- mkAscon;
   Reg#(UInt#(7)) testIndex <- mkReg(0);
   Reg#(UInt#(2)) keyIndex <- mkReg(0);
   Reg#(UInt#(8)) inputPosition <- mkReg(0);
   Reg#(UInt#(8)) outputPosition <- mkReg(0);
   Reg#(UInt#(8)) completed <- mkReg(0);
   Reg#(Bit#(32)) cycle <- mkReg(0);
   Reg#(UInt#(7)) outputPause <- mkReg(0);
   Reg#(TestPhase) phase <- mkReg(WaitCase);

   AsconTest test = asconTests[testIndex];

   rule watchdog;
      cycle <= cycle + 1;
      if (cycle == 200000)
         $fatal(1, "FAIL: Ascon testbench timed out");
   endrule

   rule skipOtherFlavor (phase == WaitCase && testIndex < fromInteger(asconTestCount)
                         && test.variantA != buildVariantA);
      testIndex <= testIndex + 1;
   endrule

   rule finish (phase == WaitCase && testIndex == fromInteger(asconTestCount));
      $display("PASS: %0d Ascon reference cases checked in %0d cycles", completed, cycle);
      $finish(0);
   endrule

   rule startCase (phase == WaitCase && testIndex < fromInteger(asconTestCount)
                   && test.variantA == buildVariantA);
      dut.start(AsconCommand { operation: test.operation, newKey: test.newKey });
      keyIndex <= 0;
      inputPosition <= 0;
      outputPosition <= 0;
      outputPause <= 100;
      if (test.newKey)
         phase <= LoadKey;
      else if (test.operation == Hash)
         phase <= SendData;
      else
         phase <= SendNonce;
   endrule

   rule sendKey (phase == LoadKey && cycle[2:0] != 3'd3);
      dut.key.put(wordAt(test.key, keyIndex));
      if (keyIndex == 3) begin
         inputPosition <= 0;
         phase <= SendNonce;
      end else
         keyIndex <= keyIndex + 1;
   endrule

   rule sendNonce (phase == SendNonce && cycle[2:0] != 3'd3);
      UInt#(8) p = inputPosition;
      Bit#(32) data = packFour(byteAt128(test.nonce, p), byteAt128(test.nonce, p + 1),
                               byteAt128(test.nonce, p + 2), byteAt128(test.nonce, p + 3));
      dut.inputWord.put(AsconInput { data: data, keep: 4'hf, last: p == 12, segment: Nonce });
      if (p == 12) begin
         inputPosition <= 0;
         phase <= SendAD;
      end else
         inputPosition <= p + 4;
   endrule

   rule sendAD (phase == SendAD && cycle[2:0] != 3'd3);
      UInt#(8) length = unpack(test.adLength);
      UInt#(8) p = inputPosition;
      Bit#(3) count = wordBytes(length, p);
      Bit#(8) b0 = count > 0 ? adByte(test, p) : 8'ha5;
      Bit#(8) b1 = count > 1 ? adByte(test, p + 1) : 8'ha5;
      Bit#(8) b2 = count > 2 ? adByte(test, p + 2) : 8'ha5;
      Bit#(8) b3 = count > 3 ? adByte(test, p + 3) : 8'ha5;
      Bool last = length == 0 || p + zeroExtend(unpack(count)) >= length;
      dut.inputWord.put(AsconInput { data: packFour(b0, b1, b2, b3), keep: keepFor(count),
                                     last: last, segment: AssociatedData });
      if (last) begin
         inputPosition <= 0;
         phase <= SendData;
      end else
         inputPosition <= p + zeroExtend(unpack(count));
   endrule

   rule sendData (phase == SendData && cycle[2:0] != 3'd3);
      UInt#(8) length = unpack(test.dataLength);
      UInt#(8) p = inputPosition;
      Bit#(3) count = wordBytes(length, p);
      Bit#(8) b0 = count > 0 ? inputByte(test, p) : 8'ha5;
      Bit#(8) b1 = count > 1 ? inputByte(test, p + 1) : 8'ha5;
      Bit#(8) b2 = count > 2 ? inputByte(test, p + 2) : 8'ha5;
      Bit#(8) b3 = count > 3 ? inputByte(test, p + 3) : 8'ha5;
      Bool last = length == 0 || p + zeroExtend(unpack(count)) >= length;
      AsconSegment segment = test.operation == Hash ? HashMessage : Text;
      dut.inputWord.put(AsconInput { data: packFour(b0, b1, b2, b3), keep: keepFor(count),
                                     last: last, segment: segment });
      if (last)
         phase <= ReadOutput;
      else
         inputPosition <= p + zeroExtend(unpack(count));
   endrule

   rule checkBusyDuringOperation (phase == SendData || (phase == ReadOutput
                                            && (outputPause != 0 || cycle[5:0] < 6'd32)));
      if (dut.idle)
         $fatal(1, "FAIL: core reported idle before the final output was consumed");
   endrule

   rule pauseOutput ((phase == SendData || phase == ReadOutput) && outputPause != 0);
      outputPause <= outputPause - 1;
   endrule

   rule readOutput ((phase == SendData || phase == ReadOutput) && outputPause == 0
                    && cycle[5:0] >= 6'd32);
      let got <- dut.outputWord.get;
      UInt#(8) offset = outputPosition;
      UInt#(8) textLength = test.operation == Hash ? 0 : unpack(test.messageLength);
      UInt#(8) boundary = test.operation == Hash || offset >= textLength
                           ? unpack(test.outputLength) : textLength;
      Bit#(3) count = wordBytes(boundary, offset);
      Bool textOutput = test.operation != Hash && offset < textLength;
      AsconOutputKind expectedKind = test.operation == Hash ? DigestOutput
                                        : textOutput ? TextOutput : TagOutput;
      Bit#(8) e0 = expectedByte(test, offset);
      Bit#(8) e1 = count > 1 ? expectedByte(test, offset + 1) : 0;
      Bit#(8) e2 = count > 2 ? expectedByte(test, offset + 2) : 0;
      Bit#(8) e3 = count > 3 ? expectedByte(test, offset + 3) : 0;
      Bit#(32) expectedData = packFour(e0, e1, e2, e3);
      Bool last = offset + zeroExtend(unpack(count)) >= boundary;
      Bool endOfOperation = offset + zeroExtend(unpack(count)) >= unpack(test.outputLength);
`ifdef XEDA_INJECT_BUG
      if (completed == 0 && offset == 0)
         expectedData[0] = ~expectedData[0];
`endif
      if (got.data != expectedData || got.keep != keepFor(count) || got.kind != expectedKind
          || got.last != last || got.endOfOperation != endOfOperation)
         $fatal(1, "FAIL: case %0d output byte %0d got %h/%b/%0d/%0d/%0d expected %h/%b/%0d/%0d/%0d",
                testIndex, offset, got.data, got.keep, got.kind, got.last, got.endOfOperation,
                expectedData, keepFor(count), expectedKind, last, endOfOperation);
      if (got.data[7:0] != e0 || (count < 2 && got.data[15:8] != 0)
          || (count < 3 && got.data[23:16] != 0) || (count < 4 && got.data[31:24] != 0))
         $fatal(1, "FAIL: case %0d has nonzero invalid output bytes", testIndex);
      if (endOfOperation) begin
         completed <= completed + 1;
         testIndex <= testIndex + 1;
         phase <= WaitCase;
      end else
         outputPosition <= offset + zeroExtend(unpack(count));
   endrule
endmodule

endpackage
