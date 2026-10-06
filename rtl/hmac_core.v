// HMAC core stub (Week 5). HMAC-SHA256 over header + window_hash, full 32-byte tag.
module hmac_core (
    input  wire clk,
    input  wire rst_n,
    input  wire [7:0] data_in,
    input  wire data_valid,
    output reg  [255:0] tag,
    output reg  tag_ready
);
  // TODO Week 5: implement.
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      tag <= 256'd0;
      tag_ready <= 1'b0;
    end else begin
      tag <= 256'd0;
      tag_ready <= 1'b0;
    end
  end
endmodule
