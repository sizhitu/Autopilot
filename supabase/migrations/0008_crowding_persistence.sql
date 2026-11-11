-- Persist the complete daily crowding snapshot, including raw short-interest inputs.
-- Safe to apply after 0007; existing position-advice rows remain valid.
ALTER TABLE public.position_advice_logs
    ADD COLUMN IF NOT EXISTS short_interest numeric,
    ADD COLUMN IF NOT EXISTS float_shares numeric,
    ADD COLUMN IF NOT EXISTS short_pct_float numeric,
    ADD COLUMN IF NOT EXISTS days_to_cover numeric,
    ADD COLUMN IF NOT EXISTS price numeric,
    ADD COLUMN IF NOT EXISTS volume numeric,
    ADD COLUMN IF NOT EXISTS avg_volume_20 numeric,
    ADD COLUMN IF NOT EXISTS history jsonb,
    ADD COLUMN IF NOT EXISTS short_history jsonb,
    ADD COLUMN IF NOT EXISTS short_behavior jsonb,
    ADD COLUMN IF NOT EXISTS published_date date,
    ADD COLUMN IF NOT EXISTS float_source text,
    ADD COLUMN IF NOT EXISTS speed_change_pct numeric,
    ADD COLUMN IF NOT EXISTS price_change_pct numeric,
    ADD COLUMN IF NOT EXISTS provider text,
    ADD COLUMN IF NOT EXISTS listing_exchange text,
    ADD COLUMN IF NOT EXISTS quality_status text;

CREATE INDEX IF NOT EXISTS idx_pos_advice_metric_type
    ON public.position_advice_logs(metric_type);
