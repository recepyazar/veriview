// Several state machines: two FSMs in this module (a request handshake and a
// one-hot phase sequencer) plus the traffic light and sequence detector FSMs
// as sub-modules. Read it with fsm.f.
module multi_fsm (
  input  logic clk, rst_n, req, ack, car, din,
  output logic busy, done, found, main_green
);
  // FSM 1: request handshake
  typedef enum logic [1:0] {H_IDLE, H_REQ, H_WAIT} hs_t;
  hs_t hs;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) hs <= H_IDLE;
    else case (hs)
      H_IDLE: if (req) hs <= H_REQ;
      H_REQ:  hs <= H_WAIT;
      H_WAIT: if (ack) hs <= H_IDLE;
      default: hs <= H_IDLE;
    endcase
  assign busy = (hs != H_IDLE);

  // FSM 2: a one-hot style 3-phase sequencer
  typedef enum logic [2:0] {P_A = 3'b001, P_B = 3'b010, P_C = 3'b100} ph_t;
  ph_t ph;
  always_ff @(posedge clk or negedge rst_n)
    if (!rst_n) ph <= P_A;
    else case (ph)
      P_A: ph <= P_B;
      P_B: if (busy) ph <= P_C;
      P_C: ph <= P_A;
      default: ph <= P_A;
    endcase
  assign done = (ph == P_C);

  traffic u_light (.clk(clk), .rst_n(rst_n), .car(car), .main_green(main_green),
                   .main_yellow(), .side_green(), .side_yellow());
  seq_fsm u_det (.clk(clk), .rst(!rst_n), .din(din), .found(found));
endmodule
