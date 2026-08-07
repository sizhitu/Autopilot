-- 方案 A：profiles 轻量登录/活跃字段（不存 IP 明文）
-- 在 Supabase SQL Editor 执行本文件即可。

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS last_login_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS last_seen_at TIMESTAMPTZ,
  ADD COLUMN IF NOT EXISTS last_login_country TEXT,
  ADD COLUMN IF NOT EXISTS login_count INTEGER NOT NULL DEFAULT 0;

COMMENT ON COLUMN public.profiles.last_login_at IS '最近一次「会话级」登录时间（12h 内重复 /me 不重复计数）';
COMMENT ON COLUMN public.profiles.last_seen_at IS '最近一次带鉴权的活跃时间';
COMMENT ON COLUMN public.profiles.last_login_country IS '最近登录时的国家/地区码（如 AU/CN，来自边缘头，非精确定位）';
COMMENT ON COLUMN public.profiles.login_count IS '会话级登录次数累计';
