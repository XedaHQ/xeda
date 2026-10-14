// written for xeda's tests
// A full adder from gates: the tiny library has no adder cell.
module \$fa (A, B, C, X, Y);
  parameter WIDTH = 1;
  input [WIDTH-1:0] A, B, C;
  output [WIDTH-1:0] X, Y;
  assign Y = A ^ B ^ C;
  assign X = (A & B) | (A & C) | (B & C);
endmodule
