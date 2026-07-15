CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS market_data (
    time TIMESTAMPTZ NOT NULL,          
    ingested_at TIMESTAMPTZ DEFAULT now(), 
    market TEXT NOT NULL,               
    zone TEXT NOT NULL,                 
    product TEXT NOT NULL,              
    value DOUBLE PRECISION,             
    source TEXT NOT NULL,               
    is_provisional BOOLEAN DEFAULT true, 
    PRIMARY KEY (time, market, zone, product)
);

SELECT create_hypertable('market_data', 'time', if_not_exists => TRUE);

ALTER TABLE market_data SET (
  timescaledb.compress,
  timescaledb.compress_segmentby = 'market,zone,product'
);