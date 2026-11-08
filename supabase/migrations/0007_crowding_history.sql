-- Autopilot crowding history: safe for both fresh and existing Supabase projects.
-- This migration is intentionally self-contained: it does not require 0006 to have run first.

CREATE TABLE IF NOT EXISTS public.position_advice_logs (
    symbol      text NOT NULL,
    bar_date    date NOT NULL,
    level       text,
    percentile  numeric,
    score       numeric,
    metric_type text,
    speed       text,
    divergence  text,
    advice      text,
    reason      text,
    source      text,
    as_of       date,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, bar_date)
);

-- Upgrade a table previously created by migration 0006.
ALTER TABLE public.position_advice_logs
    ADD COLUMN IF NOT EXISTS score numeric,
    ADD COLUMN IF NOT EXISTS metric_type text,
    ADD COLUMN IF NOT EXISTS source text,
    ADD COLUMN IF NOT EXISTS as_of date;

-- Neutral crowding snapshots are valid history even when no trade advice exists.
ALTER TABLE public.position_advice_logs
    ALTER COLUMN advice DROP NOT NULL;

CREATE INDEX IF NOT EXISTS idx_pos_advice_symbol
    ON public.position_advice_logs(symbol);
CREATE INDEX IF NOT EXISTS idx_pos_advice_date
    ON public.position_advice_logs(bar_date DESC);
CREATE INDEX IF NOT EXISTS idx_pos_advice_symbol_date
    ON public.position_advice_logs(symbol, bar_date DESC);

ALTER TABLE public.position_advice_logs ENABLE ROW LEVEL SECURITY;
