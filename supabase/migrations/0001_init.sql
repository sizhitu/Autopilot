-- ============================================================================
--  Autopilot · Supabase 初始化迁移
--  在 Supabase 控制台的 SQL Editor 中一次性执行本文件。
--
--  数据分层：
--    业务永久数据（开启 RLS，用户只能访问自己的）：
--      profiles / watchlists / analysis_history / user_preferences
--    应用级缓存（service_role 专用，不向用户开放 RLS）：
--      cache
-- ============================================================================

-- ---------------------------------------------------------------------------
--  业务表
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.profiles (
    id            uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    email         text,
    display_name  text,
    is_admin      boolean NOT NULL DEFAULT false,
    created_at    timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS public.watchlists (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id     uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    symbol      text NOT NULL,
    name        text,
    market      text,
    sort_order  integer NOT NULL DEFAULT 0,
    note        text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    UNIQUE (user_id, symbol)
);
CREATE INDEX IF NOT EXISTS idx_watchlists_user ON public.watchlists(user_id);

CREATE TABLE IF NOT EXISTS public.analysis_history (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id      uuid NOT NULL REFERENCES auth.users(id) ON DELETE CASCADE,
    symbol       text NOT NULL,
    name         text,
    result_json  jsonb NOT NULL,
    created_at   timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_analysis_user ON public.analysis_history(user_id);
CREATE INDEX IF NOT EXISTS idx_analysis_user_sym ON public.analysis_history(user_id, symbol);

CREATE TABLE IF NOT EXISTS public.user_preferences (
    user_id    uuid PRIMARY KEY REFERENCES auth.users(id) ON DELETE CASCADE,
    prefs_json jsonb NOT NULL DEFAULT '{}'::jsonb
);

-- ---------------------------------------------------------------------------
--  缓存表（应用级，仅 service_role 访问；不向终端用户开放）
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.cache (
    key         text PRIMARY KEY,
    value       jsonb NOT NULL,
    expires_at  timestamptz
);
CREATE INDEX IF NOT EXISTS idx_cache_exp ON public.cache(expires_at);

-- 客服工单：公开联系表单写入、管理员读取，均为后端 service 操作，不向终端用户开放
CREATE TABLE IF NOT EXISTS public.tickets (
    id          bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    name        text,
    email       text NOT NULL,
    country     text,
    message     text NOT NULL,
    status      text NOT NULL DEFAULT 'open',
    reply       text,
    created_at  timestamptz NOT NULL DEFAULT now(),
    replied_at  timestamptz
);
CREATE INDEX IF NOT EXISTS idx_tickets_created ON public.tickets(created_at);

-- ---------------------------------------------------------------------------
--  Row Level Security：业务表全部开启，策略统一为 auth.uid() = <用户列>
-- ---------------------------------------------------------------------------
ALTER TABLE public.profiles          ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.watchlists        ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.analysis_history  ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.user_preferences  ENABLE ROW LEVEL SECURITY;
-- cache 仅 service_role 使用；开启 RLS 并仅放行 service_role，杜绝 anon 读取
ALTER TABLE public.cache             ENABLE ROW LEVEL SECURITY;

-- profiles
DROP POLICY IF EXISTS "profiles self select" ON public.profiles;
CREATE POLICY "profiles self select" ON public.profiles
    FOR SELECT USING (auth.uid() = id);
DROP POLICY IF EXISTS "profiles self insert" ON public.profiles;
CREATE POLICY "profiles self insert" ON public.profiles
    FOR INSERT WITH CHECK (auth.uid() = id);
DROP POLICY IF EXISTS "profiles self update" ON public.profiles;
CREATE POLICY "profiles self update" ON public.profiles
    FOR UPDATE USING (auth.uid() = id) WITH CHECK (auth.uid() = id);

-- watchlists
DROP POLICY IF EXISTS "watchlists self all" ON public.watchlists;
CREATE POLICY "watchlists self all" ON public.watchlists
    FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- analysis_history
DROP POLICY IF EXISTS "analysis self all" ON public.analysis_history;
CREATE POLICY "analysis self all" ON public.analysis_history
    FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- user_preferences
DROP POLICY IF EXISTS "prefs self all" ON public.user_preferences;
CREATE POLICY "prefs self all" ON public.user_preferences
    FOR ALL USING (auth.uid() = user_id) WITH CHECK (auth.uid() = user_id);

-- cache：仅 service_role（后端管理客户端）可访问
DROP POLICY IF EXISTS "cache service only" ON public.cache;
CREATE POLICY "cache service only" ON public.cache
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- tickets：仅 service_role（后端管理客户端）可访问
DROP POLICY IF EXISTS "tickets service only" ON public.tickets;
CREATE POLICY "tickets service only" ON public.tickets
    FOR ALL TO service_role USING (true) WITH CHECK (true);

-- ---------------------------------------------------------------------------
--  触发器：新注册用户自动建 profiles 行
-- ---------------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.handle_new_user()
RETURNS trigger
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = public
AS $$
BEGIN
    INSERT INTO public.profiles (id, email, display_name)
    VALUES (
        NEW.id,
        NEW.email,
        COALESCE(NEW.raw_user_meta_data->>'display_name', split_part(NEW.email, '@', 1))
    )
    ON CONFLICT (id) DO NOTHING;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS on_auth_user_created ON auth.users;
CREATE TRIGGER on_auth_user_created
    AFTER INSERT ON auth.users
    FOR EACH ROW EXECUTE FUNCTION public.handle_new_user();
