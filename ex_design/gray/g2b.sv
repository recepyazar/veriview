// Gray code to Binary converter
module g2b #(
  parameter WIDTH = 3
) (
  input  logic [WIDTH-1:0] i_data,
  output logic [WIDTH-1:0] o_data
);
  for(genvar i=0; i<WIDTH; i++) begin
    assign o_data[i] = ^(i_data >> i);
  end
endmodule