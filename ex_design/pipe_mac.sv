// Pipelined multiply-accumulate: registered inputs, registered product and an
// accumulator. Good for timing analysis: a long multiplier path (setup), short
// input-to-register paths (hold) and an accumulator feedback loop.
module pipe_mac #(
  parameter W = 4
) (
  input  logic              clk,
  input  logic              rst_n,
  input  logic              en,
  input  logic [W-1:0]      a,
  input  logic [W-1:0]      b,
  output logic [2*W+1:0]    acc
);

  logic [W-1:0]   a_r, b_r;
  logic [2*W-1:0] prod_r;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      a_r    <= '0;
      b_r    <= '0;
      prod_r <= '0;
      acc    <= '0;
    end else begin
      a_r    <= a;
      b_r    <= b;
      prod_r <= a_r * b_r;
      if (en) acc <= acc + prod_r;
    end
  end

endmodule
