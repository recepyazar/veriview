// Mealy sequence detector for "1011" (overlapping), written as an explicit
// Verilog-2001 FSM with localparam state codes and a synchronous reset.
// `found` is a Mealy output: it depends on the state *and* the input.
module seq_fsm (
  input      clk,
  input      rst,
  input      din,
  output reg found
);

  localparam IDLE   = 2'd0,
             GOT1   = 2'd1,
             GOT10  = 2'd2,
             GOT101 = 2'd3;

  reg [1:0] state, next;

  always @(posedge clk)
    if (rst) state <= IDLE;
    else     state <= next;

  always @* begin
    next  = state;
    found = 1'b0;
    case (state)
      IDLE:   next = din ? GOT1   : IDLE;
      GOT1:   next = din ? GOT1   : GOT10;
      GOT10:  next = din ? GOT101 : IDLE;
      GOT101: if (din) begin found = 1'b1; next = GOT1; end
              else     next = GOT10;
    endcase
  end

endmodule
