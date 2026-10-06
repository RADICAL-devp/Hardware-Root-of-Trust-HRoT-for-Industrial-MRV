// Power calculation stub (Week 4). Q15 V/I in, Q30 P out. Must match Python golden model within 1 LSB.
module power_calc (
    input  wire               clk,
    input  wire               rst_n,
    input  wire signed [15:0] v_q15,
    input  wire signed [15:0] i_q15,
    output reg  signed [31:0] p_q30
);
  // TODO Week 4: implement.
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) p_q30 <= 32'sd0;
    else p_q30 <= 32'sd0;
  end
endmodule
