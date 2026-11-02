// Parameterized N:1 multiplexer with an unpacked-array data input
// (requires the sv2v or slang frontend; Yosys' built-in parser rejects unpacked array ports)
module mux #(
    parameter DATA_WIDTH = 4,
    parameter SEL_WIDTH  = 2,
    localparam NUM_INPUTS = 1 << SEL_WIDTH
) (
    input  logic [DATA_WIDTH-1:0]    i_data   [NUM_INPUTS],
    input  logic [SEL_WIDTH-1:0]     i_sel,
    output logic [DATA_WIDTH-1:0]    o_data
);

    assign o_data = i_data[i_sel];
    
endmodule