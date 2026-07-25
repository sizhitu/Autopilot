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

> 前端两种登录路径均已支持：
> - **点链接（Magic Link）**：邮件链接回跳到站点，前端 `initAuth()` 自动用 `getSessionFromUrl()` 建立会话
>   （已兼容隐式流 `#access_token=` 与 Supabase 默认 PKCE 流 `?code=` 两种回调）。
> - **手输验证码（OTP）**：在登录弹窗切换到「验证」模式，粘贴 `{{ .Token }}`，前端调用 `verifyOTP` 登录。
> 若你倾向纯 OTP（不要链接），把模板里的链接行删掉即可；纯 Magic Link 则删掉 `{{ .Token }}` 行。

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
