-- 统一订阅：plan + Stripe 字段；已注册用户可标记为 lifetime（grandfather）
-- 在 Supabase SQL Editor 执行本文件。

ALTER TABLE public.profiles
  ADD COLUMN IF NOT EXISTS plan text NOT NULL DEFAULT 'free',
  ADD COLUMN IF NOT EXISTS plan_source text NOT NULL DEFAULT 'default',
  ADD COLUMN IF NOT EXISTS stripe_customer_id text,
  ADD COLUMN IF NOT EXISTS stripe_subscription_id text,
  ADD COLUMN IF NOT EXISTS plan_expires_at timestamptz;

COMMENT ON COLUMN public.profiles.plan IS 'free | pro | lifetime';
COMMENT ON COLUMN public.profiles.plan_source IS 'default | grandfather | stripe | admin';
COMMENT ON COLUMN public.profiles.stripe_customer_id IS 'Stripe Customer ID';
COMMENT ON COLUMN public.profiles.stripe_subscription_id IS 'Stripe Subscription ID';
COMMENT ON COLUMN public.profiles.plan_expires_at IS 'pro 到期时间；lifetime/free 可为空';

-- 一次性：把执行本迁移前已存在的用户全部标为 lifetime（不受收费影响）
-- 若需改 cutoff，可改 created_at 条件后重跑（仅影响仍为 free 的行）
UPDATE public.profiles
SET plan = 'lifetime',
    plan_source = 'grandfather'
WHERE plan = 'free'
  AND plan_source = 'default'
  AND created_at < now();

-- 之后新注册用户保持 plan=free，需订阅后变为 pro
