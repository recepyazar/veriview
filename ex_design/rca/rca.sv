// WIDTH-bit ripple carry adder using full adders
module rca #(
  parameter WIDTH = 8 
) (
  input logic [WIDTH-1:0] a,
  input logic [WIDTH-1:0] b,
  input logic cin,
  output logic [WIDTH-1:0] s,
  output logic cout
);
  logic [WIDTH:0] c;  
  assign c[0] = cin;
  for (genvar i = 0; i < WIDTH; i = i + 1) begin : fa_block
    fa fa_inst (
      .a(a[i]),
      .b(b[i]),
      .cin(c[i]),
      .s(s[i]),
      .cout(c[i+1])
    );
  end
  assign cout = c[WIDTH];
endmodule