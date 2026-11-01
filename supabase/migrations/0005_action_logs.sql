-- ============================================================================
--  Autopilot · 操盘时间轴持久化
--  在 Supabase SQL Editor 中执行。
--
--  说明：
--    action_logs 是按标的共享的非观望信号历史（买卖等），不按用户隔离。
--    仅 service_role 可写；前端通过后端读取（可继续走 cache 热层）。
--    开启 RLS 且无 policy → 只有 service_role 能访问。
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.action_logs (
    symbol      text NOT NULL,
    bar_date    date NOT NULL,
    action      text NOT NULL,
    strength    integer,
    side        text,
    timing      text,
    trend       text,
    price       numeric,
    reason      text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    updated_at  timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (symbol, bar_date)
);

CREATE INDEX IF NOT EXISTS idx_action_logs_symbol ON public.action_logs(symbol);
CREATE INDEX IF NOT EXISTS idx_action_logs_date   ON public.action_logs(bar_date DESC);

ALTER TABLE public.action_logs ENABLE ROW LEVEL SECURITY;

-- 可选：允许已登录用户只读（若希望前端直接查；当前走后端即可，先不开）
-- CREATE POLICY "action_logs_select_authenticated"
--   ON public.action_logs FOR SELECT
--   TO authenticated
--   USING (true);
