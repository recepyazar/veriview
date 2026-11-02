// Binary to Gray code converter
module b2g #(
  parameter WIDTH = 3
) (
  input  logic [WIDTH-1:0] i_data,
  output logic [WIDTH-1:0] o_data
);
  assign o_data = i_data ^ (i_data >> 1);
endmodule