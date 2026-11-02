// Binary to Gray to Binary converter - optimizes to a pass-through (o_data == i_data) 
module b2g2b #(
    parameter WIDTH = 3
) (
    input  logic [WIDTH-1:0] i_data,
    output logic [WIDTH-1:0] o_data
);

    logic [WIDTH-1:0] gray;
    
    b2g #(
        .WIDTH(WIDTH)
    ) u_b2g (
        .i_data(i_data),
        .o_data(gray)
    );
    
    g2b #(
        .WIDTH(WIDTH)
    ) u_g2b (
        .i_data(gray),
        .o_data(o_data)
);
    
endmodule