// Serial sequence detector for a fixed bit pattern
module seqdect #(
  parameter PATTERN_LEN = 4,
  parameter PATTERN = 4'b1011,
  parameter bit COMB = 0 // 0 for sequential, 1 for combinational
)(
  input logic clk,
  input logic rst_n,
  input logic i_serial,
  output logic o_detected
);
  
  logic [PATTERN_LEN-1:0] shift_reg;

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      shift_reg <= '0;
    end else begin
      shift_reg <= {shift_reg[PATTERN_LEN-2:0], i_serial};
    end
  end

  if(COMB) begin : comb_block
    assign o_detected = ({shift_reg[PATTERN_LEN-2:0], i_serial} == PATTERN);
  end else begin : seq_block
    assign o_detected = (shift_reg == PATTERN);
  end

  // Elaboration-time parameter check
  if (PATTERN_LEN != $bits(PATTERN)) begin : param_check
    $error("PATTERN_LEN must equal the bit width of PATTERN");
  end

endmodule