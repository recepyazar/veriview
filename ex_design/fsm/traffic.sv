// Traffic light controller (Moore FSM) for a main road and a side road.
// The main road is green until a car waits on the side road and the minimum
// green time has passed; every green phase is followed by a yellow phase.
module traffic (
  input  logic clk,
  input  logic rst_n,
  input  logic car,          // a car is waiting on the side road
  output logic main_green,
  output logic main_yellow,
  output logic side_green,
  output logic side_yellow
);

  typedef enum logic [1:0] {
    MAIN_GO     = 2'd0,
    MAIN_YELLOW = 2'd1,
    SIDE_GO     = 2'd2,
    SIDE_YELLOW = 2'd3
  } state_t;

  state_t state, next;
  logic [2:0] timer;         // cycles spent in the current state
  logic       go;            // leave the current state

  always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      state <= MAIN_GO;
      timer <= '0;
    end else begin
      state <= next;
      timer <= go ? 3'd0 : timer + 3'd1;
    end
  end

  always_comb begin
    next = state;
    go   = 1'b0;
    case (state)
      MAIN_GO:     if (car && timer >= 3'd4)  begin next = MAIN_YELLOW; go = 1'b1; end
      MAIN_YELLOW: if (timer == 3'd1)         begin next = SIDE_GO;     go = 1'b1; end
      SIDE_GO:     if (!car || timer == 3'd5) begin next = SIDE_YELLOW; go = 1'b1; end
      SIDE_YELLOW: if (timer == 3'd1)         begin next = MAIN_GO;     go = 1'b1; end
    endcase
  end

  assign main_green  = (state == MAIN_GO);
  assign main_yellow = (state == MAIN_YELLOW);
  assign side_green  = (state == SIDE_GO);
  assign side_yellow = (state == SIDE_YELLOW);

endmodule
