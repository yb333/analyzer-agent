CREATE TABLE dws.dwb_trade_merge_d (
  order_id VARCHAR(64),
  cust_id VARCHAR(64),
  pay_amount DECIMAL(18,2),
  merge_time TIMESTAMP
);
COMMENT ON COLUMN dws.dwb_trade_merge_d.order_id IS '订单ID';
COMMENT ON COLUMN dws.dwb_trade_merge_d.cust_id IS '客户ID';
COMMENT ON COLUMN dws.dwb_trade_merge_d.pay_amount IS '支付金额';
COMMENT ON COLUMN dws.dwb_trade_merge_d.merge_time IS '合并时间';
COMMENT ON TABLE dws.dwb_trade_merge_d IS '交易增量合并表';
