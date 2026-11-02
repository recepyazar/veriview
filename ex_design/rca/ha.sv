// 2-input half adder
module ha (
  input logic a,
  input logic b,
  output logic s,
  output logic c
);
  assign c = a & b;
  assign s = a ^ b;
endmodule
