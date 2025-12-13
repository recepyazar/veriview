// Parameterized D flip-flop module
module dff #(
  parameter WIDTH = 4
) (
  input logic clk,
  input logic rst_n,
  input logic [WIDTH-1:0] d,
  output logic [WIDTH-1:0] q
);

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n)
      q <= '0;
    else
      q <= d;
  end     
    
endmodule