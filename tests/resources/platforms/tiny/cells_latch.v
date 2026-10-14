// written for xeda's tests
// A latch from a feedback path: the tiny library has no latch cell.
module $_DLATCH_P_ (input E, input D, output Q);
  assign Q = E ? D : Q;
endmodule
