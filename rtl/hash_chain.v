// SHA-256 hash chain stub (Week 5). Wraps secworks sha256 in rtl/third_party/.
module hash_chain (
    input  wire clk,
    input  wire rst_n,
    input  wire [7:0] data_in,
    input  wire data_valid,
    output reg  [255:0] window_hash,
    output reg  hash_ready
);
  // TODO Week 5: implement.
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      window_hash <= 256'd0;
      hash_ready <= 1'b0;
    end else begin
      window_hash <= 256'd0;
      hash_ready <= 1'b0;
    end
  end
endmodule
