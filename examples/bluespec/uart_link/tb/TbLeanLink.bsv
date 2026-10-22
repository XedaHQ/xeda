// Self-checking testbench for LeanLink with the test endpoint (W = 8 pages of 256 bytes).
//
// The bench plays the host at the byte interface of the link. LeanLinkFrames.bsv holds the host
// frames and the exact bytes the link must answer; the script gen_frames.py writes it from the
// codec of the host package, so no frame is computed here. The steps are:
//
//  1. The first byte out of the link after reset is a delimiter.
//  2. A request of an unknown session gets one status frame that is valid under S = 0 (UNBOUND),
//     and the endpoint executes nothing.
//  3. OPEN gets the status frame of the new session.
//  4. A request gets its exact response (CRC-32C, reverse COBS and the cursor included).
//  5. A frame with a wrong CRC, and a frame with a wrong COBS structure, get no answer and execute
//     nothing.
//  6. The same request, intact, runs once. A duplicate of it gets a status frame and no second
//     execution.
//  7. A request without payload, and a request of the largest size (the response is cut and has
//     TRUNC), get their exact responses.
//
// The link has no CRC or COBS module of its own, so the frames of the host codec test both. A
// failed check ends the run with $fatal. The top prints `PASS` when the last step is done.
// `XEDA_INJECT_BUG` makes the endpoint count each request twice, which step 4 catches.
package TbLeanLink;

import GetPut :: *;
import LeanLinkTestTop :: *;
import LeanLinkFrames :: *;

// The clocks between two bytes that the bench puts into the link (a UART at 12 Mbaud and
// 100 MHz needs 83; the link accepts a byte every 9 clocks).
Integer byteGap = 12;
// The run ends with a failure if it takes longer than this.
Integer watchdog = 200000;

(* synthesize *)
module mkTbLeanLink(Empty);
   LeanLinkTestTop dut <- mkLeanLinkTestW8;

   Reg#(UInt#(8)) ix <- mkReg(0);          // the step
   Reg#(UInt#(16)) pos <- mkReg(0);        // the byte (Send, Expect) or the clock (Quiet) of it
   Reg#(UInt#(8)) gap <- mkReg(0);         // clocks to wait before the next byte goes in
   Reg#(UInt#(32)) cycles <- mkReg(0);
   RWire#(Bit#(8)) fromLink <- mkRWire;
   PulseWire sent <- mkPulseWire;

   Bool running = ix < fromInteger(stepCount);
   Step now = steps[ix];
   UInt#(16) at = now.off + pos;

   (* fire_when_enabled, no_implicit_conditions *)
   rule noStall;
      dut.stall(False);
   endrule

   (* fire_when_enabled *)
   rule grab;
      let b <- dut.tx.get;
      fromLink.wset(b);
   endrule

   (* fire_when_enabled *)
   rule sendByte (running && now.kind == Send && gap == 0);
      dut.rx.put(frameRom[at]);
      sent.send;
   endrule

   (* fire_when_enabled, no_implicit_conditions *)
   rule step (running);
      cycles <= cycles + 1;
      if (cycles == fromInteger(watchdog))
         $fatal(1, "FAIL: no progress in step %0d at byte %0d", ix, pos);
      Bool last = pos + 1 == now.len;
      Bool next = False;
      if (isValid(fromLink.wget) && now.kind != Expect)
         $fatal(1, "FAIL: step %0d: byte %h from the link, but none is due", ix,
                fromMaybe(?, fromLink.wget));
      case (now.kind)
         Send:
            if (sent) begin
               gap <= fromInteger(byteGap - 1);
               pos <= last ? 0 : pos + 1;
               next = last;
            end
            else if (gap != 0) gap <= gap - 1;
         Expect:
            if (fromLink.wget matches tagged Valid .b) begin
               if (b != frameRom[at])
                  $fatal(1, "FAIL: step %0d: byte %0d is %h, expected %h", ix, pos, b, frameRom[at]);
               pos <= last ? 0 : pos + 1;
               next = last;
            end
         Quiet: begin
            pos <= last ? 0 : pos + 1;
            next = last;
         end
         Count: begin
            if (dut.executed != extend(pack(now.len)))
               $fatal(1, "FAIL: step %0d: the endpoint executed %0d requests, expected %0d", ix,
                      dut.executed, now.len);
            next = True;
         end
      endcase
      if (next) ix <= ix + 1;
   endrule

   (* fire_when_enabled *)
   rule finish (!running);
      $display("PASS: LeanLink W=8: %0d steps in %0d clocks", stepCount, cycles);
      $finish(0);
   endrule
endmodule

endpackage
