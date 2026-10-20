package TbLinkCodecs;

import Cobs :: *;
import Crc32c :: *;
import GetPut :: *;
import StmtFSM :: *;

(* synthesize *)
module mkTbLinkCodecs(Empty);
   CobsDecoder decoder <- mkCobsDecoder;
   RcobsEncoder encoder <- mkRcobsEncoder;
   Crc32c crc1 <- mkCrc32c(1);
   Crc32c crc2 <- mkCrc32c(2);
   Crc32c crc4 <- mkCrc32c(4);
   Crc32c crc8 <- mkCrc32c(8);
   Reg#(UInt#(16)) cycles <- mkReg(0);
   Reg#(UInt#(16)) decoded <- mkReg(0);
   Reg#(UInt#(16)) encoded <- mkReg(0);
   Reg#(UInt#(16)) i <- mkReg(0);
   Reg#(Bool) pauseDecoder <- mkReg(False);
   Bit#(16) cycleBits = pack(cycles);

   rule tick;
      cycles <= cycles + 1;
      if (cycles == 10000) begin
         $display("FAIL: codec test timed out (%0d decoded, %0d encoded)", decoded, encoded);
         $fatal(1, "FAIL: codec check");
      end
   endrule

   function CobsToken expectedDecoded(UInt#(16) n);
      case (n)
         0: return tagged End True;
         1: return tagged Byte 8'h11;
         2: return tagged Byte 8'h22;
         3: return tagged Byte 0;
         4: return tagged Byte 8'h33;
         5: return tagged End True;
         6: return tagged Byte 8'h44;
         7: return tagged End False;
         8: return tagged Byte 0;
         9: return tagged End True;
         264: return tagged End True;
         265: return tagged Byte 8'h66;
         266: return tagged End True;
         default: return tagged Byte 8'h55;
      endcase
   endfunction

   function Bit#(8) expectedEncoded(UInt#(16) n);
      case (n)
         0: return 8'h11;
         1: return 8'h22;
         2: return 3;
         3: return 8'h33;
         4: return 2;
         5: return 0;
         6: return 1;
         7: return 0;
         262: return 8'hff;
         263: return 1;
         264: return 0;
         265: return 1;
         266: return 1;
         267: return 0;
         default: return 8'h55;
      endcase
   endfunction

   rule checkDecoder (cycleBits[1:0] != 0 && !pauseDecoder);
      let actual <- decoder.out.get;
      if (decoded >= 267 || actual != expectedDecoded(decoded)) begin
         $display("FAIL: decoded token %0d: ", decoded, fshow(actual));
         $fatal(1, "FAIL: codec check");
      end
      decoded <= decoded + 1;
   endrule

   rule checkEncoder (cycleBits[2:0] != 0);
      let actual <- encoder.out.get;
      if (encoded >= 268 || actual != expectedEncoded(encoded)) begin
         $display("FAIL: encoded byte %0d: %02x", encoded, actual);
         $fatal(1, "FAIL: codec check");
      end
      encoded <= encoded + 1;
   endrule

   Stmt test = seq
      decoder.in.put(0);
      decoder.in.put(1); decoder.in.put(0);
      decoder.in.put(3); decoder.in.put('h11); decoder.in.put('h22);
      decoder.in.put(2); decoder.in.put('h33); decoder.in.put(0);
      decoder.in.put(3); decoder.in.put('h44); decoder.in.put(0);
      decoder.in.put(1); decoder.in.put(1); decoder.in.put(0);
      decoder.in.put('hff);
      for (i <= 0; i < 254; i <= i + 1) decoder.in.put('h55);
      decoder.in.put(1); decoder.in.put(0);
      await (decoded == 265);
      pauseDecoder <= True;
      decoder.in.put(3); decoder.in.put('h99);
      repeat (4) noAction;
      decoder.abort;
      pauseDecoder <= False;
      decoder.in.put('h77); decoder.in.put(0);
      decoder.in.put(2); decoder.in.put('h66); decoder.in.put(0);
      encoder.in.put(tagged Byte 'h11); encoder.in.put(tagged Byte 'h22);
      encoder.in.put(tagged Byte 0); encoder.in.put(tagged Byte 'h33);
      encoder.in.put(tagged End True);
      encoder.in.put(tagged End True);
      for (i <= 0; i < 254; i <= i + 1) encoder.in.put(tagged Byte 'h55);
      encoder.in.put(tagged End True);
      encoder.in.put(tagged Byte 0); encoder.in.put(tagged End True);
      for (i <= 0; i < 9; i <= i + 1) action
         Bit#(8) value = 8'h31 + truncate(pack(i));
         crc1.put(value); crc2.put(value); crc4.put(value); crc8.put(value);
      endaction
      await (crc1.ready && crc2.ready && crc4.ready && crc8.ready);
      action
         if (crc1.state != 'h1cf96d7c || crc2.state != 'h1cf96d7c ||
             crc4.state != 'h1cf96d7c || crc8.state != 'h1cf96d7c) begin
            $display("FAIL: CRC-32C known vector: %08x %08x %08x %08x",
                     crc1.state, crc2.state, crc4.state, crc8.state);
            $fatal(1, "FAIL: codec check");
         end
      endaction
      action crc1.clear; crc2.clear; crc4.clear; crc8.clear; endaction
      for (i <= 0; i < 13; i <= i + 1) action
         Bit#(8) value = 8'h31 + truncate(pack(i));
         case (i)
            9: value = 'h83;
            10: value = 'h92;
            11: value = 'h06;
            12: value = 'he3;
         endcase
         crc1.put(value); crc2.put(value); crc4.put(value); crc8.put(value);
      endaction
      await (crc1.ready && crc2.ready && crc4.ready && crc8.ready);
      action
         if (crc1.state != 'hb798b438 || crc2.state != 'hb798b438 ||
             crc4.state != 'hb798b438 || crc8.state != 'hb798b438) begin
            $display("FAIL: CRC-32C residue");
            $fatal(1, "FAIL: codec check");
         end
      endaction
      await (decoded == 267 && encoded == 268);
      $display("PASS: codecs, malformed frame, full block, abort, stalls and CRC-32C residue");
      $finish(0);
   endseq;
   mkAutoFSM(test);
endmodule

endpackage
