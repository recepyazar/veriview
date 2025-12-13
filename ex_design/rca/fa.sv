// 1-bit full adder
module fa (
  input logic a,
  input logic b,
  input logic cin,
  output logic s,
  output logic cout
);
  logic s1, c1, c2;

  ha ha1 (
    .a(a),
    .b(b),
    .s(s1),
    .c(c1)
  );

  ha ha2 (
    .a(s1),
    .b(cin),
    .s(s),
    .c(c2)
  );

  assign cout = c1 | c2;  
endmodule