-- Neutral crowding snapshots must be persisted even when no trade advice is triggered.
ALTER TABLE public.position_advice_logs
  ALTER COLUMN advice DROP NOT NULL;

ALTER TABLE public.position_advice_logs
  ADD COLUMN IF NOT EXISTS score numeric,
  ADD COLUMN IF NOT EXISTS metric_type text,
  ADD COLUMN IF NOT EXISTS source text,
  ADD COLUMN IF NOT EXISTS as_of date;

CREATE INDEX IF NOT EXISTS idx_pos_advice_symbol_date
  ON public.position_advice_logs(symbol, bar_date DESC);
