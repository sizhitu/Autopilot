-- ============================================================================
--  Autopilot · 个股仓位建议时间轴持久化
--  在 Supabase SQL Editor 中执行。
--
--  说明：
--    position_advice_logs 记录个股拥挤度三维度触发的仓位建议。
--    仅 service_role 可写；前端通过后端读取。
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.position_advice_logs (
    symbol      text NOT NULL,
    bar_date    date NOT NULL,
    level       text,
    percentile  numeric,
    speed       text,
    divergence  text,
    advice      text NOT NULL,
    reason      text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, bar_date)
);

CREATE INDEX IF NOT EXISTS idx_pos_advice_symbol ON public.position_advice_logs(symbol);
CREATE INDEX IF NOT EXISTS idx_pos_advice_date   ON public.position_advice_logs(bar_date DESC);

ALTER TABLE public.position_advice_logs ENABLE ROW LEVEL SECURITY;
