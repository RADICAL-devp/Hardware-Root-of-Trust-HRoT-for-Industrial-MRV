// Attestation core top level stub (Week 5). UART RX + power calc + hash chain + HMAC + counter.
module top (
    input  wire clk,
    input  wire rst_n,
    input  wire rx,
    output wire [255:0] hmac_tag,
    output wire tag_ready
);
  // TODO Week 5: implement.
  assign hmac_tag = 256'd0;
  assign tag_ready = 1'b0;
endmodule
