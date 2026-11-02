// 2:1 multiplexer
module mux_basic (
    input logic d0,
    input logic d1,
    input logic sel,
    output logic y
);
    assign y = sel ? d1 : d0;
endmodule