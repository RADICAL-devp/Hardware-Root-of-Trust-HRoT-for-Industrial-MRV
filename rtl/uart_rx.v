// UART receiver stub (Week 4). Receives L0 SampleFrames: SOF 0xA5 + counter + V + I + CRC16.
module uart_rx (
    input  wire clk,
    input  wire rst_n,
    input  wire rx,
    output reg  [7:0] data,
    output reg  valid
);
  // TODO Week 4: implement.
  always @(posedge clk or negedge rst_n) begin
    if (!rst_n) begin
      data <= 8'd0;
      valid <= 1'b0;
    end else begin
      data <= 8'd0;
      valid <= 1'b0;
    end
  end
endmodule
