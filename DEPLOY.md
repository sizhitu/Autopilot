# 部署指南（Supabase 架构版）

> 架构：**前端 Cloudflare Pages（静态 + CDN）** + **后端 Render（FastAPI + pandas/numpy）** + **Supabase（Auth / Postgres + RLS）** + **Upstash Redis（缓存层）** + **外部行情 API（Yahoo / 新浪 / Eastmoney 等）**
>
> 认证完全交给 **Supabase Auth**（邮箱 OTP 验证码 / Magic Link），业务数据存 **Supabase Postgres** 并开启 **RLS**（用户只能访问自己的数据）。
> 未配置 `SUPABASE_*` 时，后端**自动回退 SQLite**，本地开发与沙箱冒烟照常可跑。

```
浏览器 ──(HTTPS)──▶ Cloudflare Pages  (frontend/index.html, 纯静态, CDN)
                        │  supabase-js 直连 Supabase Auth（OTP / Magic Link）
                        ▼ fetch('/api/...') 带 Bearer(JWT)
                  Render  Web Service  (trading_tool/, FastAPI)
                        │  PyJWT 校验 JWT → 注入 access_token 走 RLS
                        ├── Supabase Postgres（profiles / watchlists / analysis_history，开启 RLS）
                        ├── Upstash Redis（实时行情 / AI 报告缓存，TTL 分层）
                        └── 外部行情源：Yahoo / 新浪 / Nasdaq / Eastmoney
```

---

## 一、Supabase 初始化（必做）

1. 登录 [supabase.com](https://supabase.com) → 新建项目，记下 **Project URL** 与 **anon key / service_role key / JWT Secret**（Project Settings → API）。
2. 打开 **SQL Editor**，把本仓库 `supabase/migrations/0001_init.sql` 整段粘贴执行。
   该脚本会建好全部业务表并开启 RLS：
   - `profiles` / `watchlists` / `analysis_history` / `user_preferences`（业务永久数据，**全部开启 RLS**，`auth.uid() = user_id`）
   - `cache`（应用级缓存，**仅 service_role 可访问**，绝不向终端用户开放）
   - `tickets`（客服工单，仅 service_role）
   - 触发器：新注册用户自动建 `profiles` 行
3. **人工核对 RLS**（上线前必查）：每张业务表的策略必须 `USING (auth.uid() = user_id)` 且 `WITH CHECK` 同条件；`cache` / `tickets` 仅 `TO service_role`。

> ⚠️ 安全红线：`SUPABASE_SERVICE_ROLE_KEY` 仅在后端环境变量里，**严禁**进前端、严禁进代码仓库。

---

## 二、认证邮件（Supabase Auth → Resend）

1. Supabase 控制台 → **Authentication → Providers → Email**，在「Custom SMTP」处选择 **Resend** 并填入 Resend 的 API Key 与发信域名（如 `noreply@timebricks.bid`）。
   - 这样注册 / 登录的 **OTP 验证码** 与 **Magic Link** 由 Supabase 经 Resend 真实发出。
2. 应用层邮件（EDM 群发 / 客服工单通知）由后端 `trading_tool/mailer.py` 直连 **Resend REST API**（`RESEND_API_KEY`）。
   - 未配 `RESEND_API_KEY` 时回退 SMTP（开发用，见旧 SMTP 配置）；但**生产建议一律走 Resend**。

### 3) 认证邮件模板（双模式：链接 + 验证码）

前端统一用 `signInWithOtp` 发信；为兼容「点击链接」与「手动输入验证码」两种登录方式，
请在 Supabase 控制台 **Authentication → Email Templates**（Sign In / Sign Up 模板）中放入**链接 + 验证码**：

```html
<h2>登录验证</h2>
<p>你的验证码：<strong>{{ .Token }}</strong></p>
<p>或者直接点击链接登录：</p>
<p><a href="{{ .ConfirmationURL }}">点击登录</a></p>
```

> 前端 **两种登录方式并存**，弹窗顶部可切换：
> - **🔑 密码登录（默认，推荐日常使用）**：`signInWithPassword` / `signUp`，
>   注册时设密码、可选昵称；之后日常登录只需邮箱 + 密码，**无需每次查收邮件**。
>   （若曾用验证码注册、尚未设密码，`signInWithPassword` 会提示改用「邮箱验证码」登录。）
> - **✉️ 邮箱验证码 / Magic Link（无密码）**：`signInWithOtp` 发信，回跳后：
>   - **点链接（Magic Link）**：`supabase-js` 用 **隐式流（`flowType: 'implicit'`）**，链接形如 `#access_token=...`，
>     `initAuth()` 用 `getSessionFromUrl()` 直接建会话，**跨设备 / 跨浏览器点击也能登录**
>     （PKCE 流依赖同浏览器的 verifier，手机/另一台电脑点链接会失效，故不用）。
>     `createClient` 设 `detectSessionInUrl: false`，由前端手动接管回跳，把 token 写入自有状态（`authToken`）。
>   - **手输验证码（OTP）**：粘贴邮件里的 `{{ .Token }}`，前端调用 `verifyOtp`（v2 小写 t）登录。
>
> 若只想要其中一种：纯 OTP 把模板链接行删掉；纯 Magic Link 把 `{{ .Token }}` 行删掉；纯密码则把发信逻辑换成 `signUp`/`signInWithPassword`（已内置）。

> **关于「Token has expired or is invalid」**：OTP 验证码有时效（Supabase Auth → Providers → Email → OTP expiry，默认 1 小时），且 `{{ .Token }}` 必须放在 **Magic Link 邮件模板** 中（不是 Confirm signup 模板）。类型也要匹配：`signInWithOtp({shouldCreateUser:true})` 对应 `verifyOtp({type:'signup'})`，否则 `type:'email'`。

> **重定向 URL（关键）**：前端 `emailRedirectTo` 统一用 `window.location.origin`（即站点根域名，如 `https://www.timebricks.bid`），
> 务必在 Supabase **Authentication → URL Configuration → Redirect URLs** 中加入 `https://www.timebricks.bid`（含 `www`、https、无多余路径/斜杠），
> 否则 Magic Link 回跳会被 Supabase 以 redirect_uri 不匹配拒绝，表现为「点了链接却没登录」。

> **Magic Link 排障（用户实测：点链接跳首页、无用户信息）**
> 你贴的链接形如：
> `https://<project>.supabase.co/auth/v1/verify?token=...&type=magiclink&redirect_to=https://www.timebricks.bid`
> 这条链接**格式本身是对的**（`type=magiclink` + `redirect_to=站点根域名`）。点开后跳回首页却没登录，根因在 Supabase 控制台配置，逐项核对：
> 1. **Redirect URLs 必须含 `https://www.timebricks.bid`**：`Authentication → URL Configuration → Redirect URLs` 加入该精确地址（带 `www`、https、无尾斜杠）。缺这一项时 Supabase 会拒绝回跳，直接落到首页、不带 token。
> 2. **Site URL 设为 `https://www.timebricks.bid`**：同页面 `Site URL` 字段，保证默认回跳落点正确。
> 3. **Auth 流类型设为 Implicit（保证跨设备可用）**：`Authentication → Providers`（或 URL Configuration）里的 `Auth flow type` 选 **Implicit**。前端 `flowType:'implicit'` 已就位——隐式流把 `#access_token=...` 直接带在 URL 片段里，手机/另一台电脑点链接也能直接换到会话；若保持默认的 **PKCE**，链接回跳是 `?code=...`，依赖**同一浏览器**的 `code_verifier`，换设备点击必失效。前端 `initAuth()` 两种流都已兼容（`#access_token` 与 `?code=` 都解析）。
> 4. **确认线上构建已含 implicit 流**：本仓库 `frontend/index.html` 的 `createClient(..., { auth: { flowType:'implicit', detectSessionInUrl:false } })` 已推送（commit `73f7803`）。Cloudflare Pages 从 `main` 自动部署后，请**硬刷新**（Ctrl/Cmd+Shift+R）清掉旧缓存再测。
> 5. **链接有时效**：`type=magiclink` 的 token 也有有效期（默认 1 小时，见 `Authentication → Providers → Email`），过期同样会跳首页无会话，重新获取即可。

> **刷新页面被登出 / 注册确认后没登录（代码层已修复，记录根因）**
> 之前的「刷新即登出」根因不在逻辑分支，而是一个**自毁陷阱**：`apiJSON()` 在收到后端 `401` 时会调用 `doLogout()` 清空本地会话；而 `finishSession()` 用 `apiJSON('/api/auth/me')` 去校验会话，于是「恢复会话」这一步本身就会因令牌过期/后端抖动触发登出。
> 修复方式（commit `f8692bb`）：把 supabase 会话视为登录态**真值来源**——`finishSession()` 以 `session.user` 为基线，仅用 `api()`（非 `apiJSON`）补全 `display_name/is_admin`，`401`/失败一律保留会话；`initAuth()` 恢复时先 `getUser()` 强制用 refresh token 续期、再取续期后的会话，彻底杜绝过期令牌误登出。注册确认链接建立会话的最后一步用的是同一 `finishSession`，因此同步受益。

> **自选看板刷新体验**：`/api/watchlist` 永远秒回。首次或「刷新」时后台并行抓取，每算完一只就增量写回缓存，
> 前端轮询渲染**已就绪的行**（右下角显示「加载中 X/N…」），全部算完才停止轮询；登录但尚无自选的用户直接复用已预热的默认看板，即时展示。

---

## 三、缓存层（Upstash Redis，可选但推荐）

实时行情、AI 分析结果属高频临时数据，不应大量落业务库。配置 `UPSTASH_REDIS_REST_URL` / `UPSTASH_REDIS_REST_TOKEN` 后：
- 实时行情缓存 TTL 短（`CACHE_QUOTE_TTL`，默认 300s）
- AI 报告 / 每日 K 线缓存 TTL 中长（`CACHE_REPORT_TTL`，默认 21600s = 6h）

未配 Upstash 但配了 Supabase 时，自动复用 Postgres `cache` 表；两者皆无时回退进程内内存（仅单实例开发用）。

---

## 四、后端 → Render

1. 登录 [render.com](https://render.com) → **New → Web Service** → 连接 GitHub 仓库 `sizhitu/Autopilot`。
2. 部署方式选择 **`render.yaml`**（自动读取根目录配置）；或手动：
   - **Root Directory**: `trading_tool`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn web_app:app --host 0.0.0.0 --port $PORT`
   - **Health Check Path**: `/api/health`
   - **Instance Type**: Free
3. **Environment**（在 `render.yaml` 里已声明，sync:false 表示需在 Render 控制台手动填入真实值）：

| 变量 | 默认 / 说明 |
|------|------|
| `SUPABASE_URL` | 项目 URL（生产必填，否则回退 SQLite） |
| `SUPABASE_ANON_KEY` | anon key（前端公开，受 RLS 约束） |
| `SUPABASE_SERVICE_ROLE_KEY` | service_role key（**仅后端**，绕过 RLS，**严禁进前端**） |
| `SUPABASE_JWT_SECRET` | 用于后端校验前端 JWT（HS256） |
| `UPSTASH_REDIS_REST_URL` / `_TOKEN` | 缓存层（可选） |
| `RESEND_API_KEY` | 应用邮件（可选，否则回退 SMTP） |
| `SUPPORT_EMAIL` | `support@timebricks.bid` 客服工单收件 |
| `SITE_NAME` | `Autopilot 投资分析` 邮件标题前缀 |
| `ADMIN_EMAILS` | 额外管理员邮箱（逗号分隔） |
| `CORS_ORIGINS` | `https://timebricks.bid,https://你的pages子域.pages.dev` |
| `DATABASE_PATH` | `/tmp/autopilot.db`（仅本地/未配 Supabase 时使用的 SQLite 回退；注释保留） |
| `ENABLE_REALTIME` | `false`（多设备实时同步开关，默认关；设 `true` 开启 Supabase Realtime 订阅自选变更） |
| `CACHE_QUOTE_TTL` / `CACHE_REPORT_TTL` | 300 / 21600 |

4. 部署完成后记下后端地址 `https://autopilot-api.onrender.com`。

> 验证：`https://<后端>/api/health` 返回 `{"success":true,...}`；`/api/config` 的 `using_supabase` 在配好 Supabase 后为 `true`。

---

## 五、前端 → Cloudflare Pages

1. Cloudflare → **Workers & Pages → Create → Pages → 连接 GitHub**。
2. 仓库 `sizhitu/Autopilot`，构建设置：
   - **Framework preset**: `None`
   - **Build command**: 留空
   - **Build output directory**: `frontend`
3. 部署后得 `*.pages.dev`；到 **Custom domains** 绑定 `timebricks.bid`。
4. 前端通过 `/api/config` 自动拿到 `supabase_url` / `anon_key` 初始化 `supabase-js`，无需在前端硬编码密钥。

> 旧版 `timebricks.bid` 上跑的是已废弃的 Cloudflare Worker（1101 报错）。请到 Cloudflare **Workers** 删除旧 Worker，再把 `timebricks.bid` 作为自定义域绑定到本 Pages 项目。

---

## 六、本地开发（SQLite 回退）

```bash
cd trading_tool
pip install -r requirements.txt
# 不设置 SUPABASE_* → 自动回退 SQLite，本地照常可跑
export DATABASE_PATH=./dev.db
uvicorn web_app:app --reload --port 8000

# 另开终端预览前端（默认指向上面的本地后端）
cd ../frontend
python3 -m http.server 5500
# 浏览器打开 http://localhost:5500
```

本地登录：前端走 Supabase Auth（需配 Supabase 才能真实收发邮件）；若只想本地自测接口，后端在 `using_supabase()==False` 时支持 **dev token**——请求头 `Authorization: Bearer dev:<任意uid>` 会被当作本地开发用户（管理员），仅限本地、绝不进生产。

---

## 七、新增能力速览（Supabase 版）

| 能力 | 说明 |
|------|------|
| 认证 | 前端 supabase-js 走 **邮箱 OTP 验证码 / Magic Link**；后端仅用 PyJWT 校验 JWT 并同步 `profiles` |
| 自选看板 | `/api/watchlist` 按用户排序 + 备注；`/api/watchlist/add` `remove` `reorder` `note`；未登录回退默认看板 |
| 多市场 | `symbols.normalize_symbol` 统一格式：港股(`00700.HK`) / A股(6位) / 美股(字母) / 指数(`^IXIC`) |
| 分析历史 | `/api/quote` `/api/analyze` 成功后自动写入 `analysis_history`（关联 user+symbol）；`/api/history` 可分页回溯 |
| 接口限流 | `/api/quote /search /backtest /analyze` 固定窗口限流（Upstash 优先，否则进程内），超限返回 `429` 友好提示 |
| 容错 | 实时行情拉取失败 → 回退行情缓存 → 回退每日 K 线缓存，并标记 `stale`；前端提示「数据可能延迟」 |
| 缓存分层 | 原始 K 线只进缓存层（Redis / `cache` 表），**不大量写业务库**（严格分离） |
| 多设备同步 | `ENABLE_REALTIME=true` 时前端订阅 `watchlists` 变更自动刷新看板（默认关） |
| AI 周/月报 | 定时为每位用户生成关注股票总结邮件（周报只陈述事实、月报含趋势与操作参考）；AI 缺失时降级为结构化 HTML |

---

## 八、管理员后台 · EDM 群发 · 客服工单

1. **管理员身份**：`ADMIN_EMAILS` 命中即管理员；登录后顶部出现「管理」标签。
2. **SMTP 真实发信（后台可配置）**：管理 → SMTP 配置 写入 `settings` 表（优先级高于环境变量），保存后自动发测试邮件。未配置则回退开发模式（或走 Resend）。
3. **用户管理与 EDM**：`GET /api/admin/stats`、`GET /api/admin/users`、`POST /api/admin/edm/send`（向全部/已验证用户群发）。
4. **客服咨询 + 自动工单**：右下角「💬 客服咨询」提交 → `POST /api/contact` 写入 `tickets` 并发邮件到 `SUPPORT_EMAIL`；管理员可回复并邮件通知客户。

---

## 八之二、AI 周报 / 月报（定时生成并群发）

每个周末为**每位用户**自动生成一份关注股票的分析总结邮件：

- **分析对象**：有自选 → 其自选股；无自选 → 回退「未登录默认看板」股票列表（`watchlist.WATCHLIST`）。
- **数据来源**：藤本茂策略信号 / 神奇九转 / 估值 / 高低，由 `trading_tool/reports.py` 拉取行情后计算。
- **周报**：仅陈述事实，**不给买卖建议**（AI prompt 强制约束）。
- **月报**：含当下趋势、机会与风险、操作参考（标注为「参考」）。
- **AI 缺失降级**：未配 `AI_API_KEY` 时，报告自动降级为结构化纯文本 HTML，主流程不中断。
- **投资建议（后续）**：回测数据、策略占比、收益预测等投资建议能力预留，本期未实现。

### 1) 配置 AI（OpenAI 兼容）

| 变量 | 默认 | 说明 |
|------|------|------|
| `AI_API_KEY` | 空 | 必填才启用 AI 润色；可对接 OpenAI / DeepSeek / 通义千问 / 智谱 GLM / 本地 vLLM（改下一项） |
| `AI_BASE_URL` | `https://api.openai.com/v1` | 兼容 OpenAI Chat Completions 的基址 |
| `AI_MODEL` | `gpt-4o-mini` | 模型名 |
| `REPORT_MAX_SYMBOLS` | `15` | 单封报告最多分析标的数量 |

### 2) 触发方式

- **手动 / 测试**：管理员调用 `POST /api/admin/reports/generate`
  - `{"period":"weekly"}` 遍历全部用户群发；`{"period":"weekly","email":"x@y.com"}` 仅给该邮箱生成一封（预览）。
- **命令行**：`cd trading_tool && python reports.py weekly`（或 `monthly`）。
- **定时（生产）**：用 **Render Cron Job**（或 GitHub Actions scheduled workflow）每周末调用上面的命令 / 接口。
  例：Render → Cron Job → Command `cd trading_tool && python reports.py weekly`，Schedule `0 9 * * 0`（每周日 9 点，UTC）。
  月报单独建一个 Cron：`0 9 1 * *`（每月 1 号）。

> 依赖发信：`reports.py` 复用 `mailer.send_email`（优先 Resend，回退 SMTP），需 `RESEND_API_KEY` 或 SMTP 配置可用。

---

## 九、上线自查清单（Supabase 相关）

- [ ] `0001_init.sql` 已在 Supabase SQL Editor 执行成功
- [ ] 业务表 RLS 策略均为 `auth.uid() = user_id`（USING + WITH CHECK）
- [ ] `cache` / `tickets` 仅 `service_role` 可访问
- [ ] `SUPABASE_SERVICE_ROLE_KEY` 仅存在于 Render 环境变量，**未进前端/仓库**
- [ ] Supabase Auth 邮件投递已绑定 Resend（能真实收到 OTP / Magic Link）
- [ ] `RESEND_API_KEY` 已配置（应用邮件）
- [ ] `UPSTASH_REDIS_*` 已配置（或接受 `cache` 表回退）
- [ ] `CORS_ORIGINS` 包含前端域名
- [ ] `render.yaml` 中 `sync:false` 的变量已在 Render 控制台填好真实值
- [ ] `/api/config` 返回 `using_supabase: true`
- [ ] 改完环境变量后 **Manual Deploy 一次** 让配置在启动时加载
