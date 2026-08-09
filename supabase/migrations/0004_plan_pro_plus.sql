-- Basic→Pro 命名；老用户（lifetime/grandfather）统一为 plus 权益展示（plan 可保留 lifetime）
-- 可选执行：把仍是 basic 的写成 pro
UPDATE public.profiles SET plan = 'pro' WHERE plan = 'basic';
-- 可选：明确把 grandfather 标成 plus（若希望角标显示 Plus 而不是终身）
-- UPDATE public.profiles SET plan = 'plus' WHERE plan_source = 'grandfather' AND plan = 'lifetime';
