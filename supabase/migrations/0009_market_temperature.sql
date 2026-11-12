-- Daily market sentiment snapshots; service-role writes, API-mediated public reads.
CREATE TABLE IF NOT EXISTS public.market_temperature_daily (
 market text NOT NULL CHECK (market IN ('cn','us')),
 trade_date date NOT NULL,
 temperature numeric,
 regime text NOT NULL,
 coverage numeric NOT NULL DEFAULT 0,
 method_version text NOT NULL DEFAULT '1.0.0',
 components jsonb NOT NULL DEFAULT '{}'::jsonb,
 snapshot jsonb NOT NULL DEFAULT '{}'::jsonb,
 calculated_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (market,trade_date)
);
CREATE INDEX IF NOT EXISTS idx_market_temperature_date ON public.market_temperature_daily(trade_date DESC,market);
ALTER TABLE public.market_temperature_daily ENABLE ROW LEVEL SECURITY;
