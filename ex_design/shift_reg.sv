// Parameterized shift register: NUM_REGS stages of DATA_WIDTH-bit flip-flops
// with asynchronous active-low reset and a common shift enable.
// q[0] is the first stage, q[NUM_REGS-1] the last.
module shift_reg #(
  parameter DATA_WIDTH = 4,
  parameter NUM_REGS   = 2
) (
  input  logic                  clk,
  input  logic                  rst_n,
  input  logic                  en,
  input  logic [DATA_WIDTH-1:0] d,
  output logic [DATA_WIDTH-1:0] q [NUM_REGS]
);

  // Elaboration-time parameter check
  if (NUM_REGS < 1) begin : param_check
    $error("NUM_REGS must be >= 1");
  end

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n)
      q[0] <= '0;
    else if (en)
      q[0] <= d;
  end

  for (genvar i = 1; i < NUM_REGS; i++) begin : gen_stage
    always_ff @(posedge clk or negedge rst_n) begin
      if (!rst_n)
        q[i] <= '0;
      else if (en)
        q[i] <= q[i-1];
    end
  end

endmodule
