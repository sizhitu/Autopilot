# 部署指南（前后端分离版）

> 架构：**前端 Cloudflare Pages（静态 + CDN）** + **后端 Render（FastAPI + pandas/numpy + SQLite）**
>
> 之前「Cloudflare Workers / Containers 免费部署 Python」的方案已废弃——
> 免费 Worker 有 5MB 体积限制、无法运行 Python/pandas，物理上跑不了本项目的后端。

```
浏览器 ──(HTTPS, 跨域 fetch)──▶ Cloudflare Pages  (frontend/index.html, 纯静态, CDN)
                                       │
                                       ▼ fetch('/api/...') 带 Bearer 令牌
                                 Render  Web Service  (trading_tool/, FastAPI)
                                       │
                                       ├── 行情源：Yahoo / 新浪 / Nasdaq / Eastmoney
                                       └── SQLite：用户 / 会话 / 自选看板 / 每日行情
```

---

## 一、后端 → Render

1. 登录 [render.com](https://render.com) → **New → Web Service** → 连接 GitHub 仓库 `sizhitu/Autopilot`。
2. 在部署方式里**选择 `render.yaml`**（会自动读取本仓库根目录配置）；或手动填：
   - **Root Directory**: `trading_tool`
   - **Build Command**: `pip install -r requirements.txt`
   - **Start Command**: `uvicorn web_app:app --host 0.0.0.0 --port $PORT`
   - **Health Check Path**: `/api/health`
   - **Instance Type**: Free
3. 在 **Environment** 里设置（SMTP 可暂不填，先用开发模式跑通）：
   - `DATABASE_PATH` = `/tmp/autopilot.db`（免费版临时盘，重启清空；持久化请挂 Render Disk 或改用 Postgres）
   - `CORS_ORIGINS` = `https://timebricks.bid,https://你的pages子域.pages.dev`
   - `SMTP_HOST` / `SMTP_PORT` / `SMTP_USER` / `SMTP_PASS` / `SMTP_FROM`（见下文邮件配置）
4. 部署完成后，记下后端地址，形如 `https://autopilot-api.onrender.com`。

> 验证：浏览器打开 `https://<你的后端>/api/health` 应返回 `{"success":true,...}`。

---

## 二、前端 → Cloudflare Pages

1. 登录 Cloudflare → **Workers & Pages → Create → Pages → 连接 GitHub**。
2. 选择仓库 `sizhitu/Autopilot`，构建设置：
   - **Framework preset**: `None`
   - **Build command**: 留空
   - **Build output directory**: `frontend`   ← 关键，指向静态目录
3. 部署完成后得到 `*.pages.dev` 地址；再到 **Custom domains** 绑定 `timebricks.bid`。
4. 在前端页面右上角点 **⚙** 可手动填写后端地址（存 localStorage），或用 `?api=https://你的后端` 覆盖。
   - 也可直接改 `frontend/index.html` 里的默认 `BACKEND`（搜索 `autopilot-api.onrender.com` 替换为你真实地址）。

> 注意：之前 `timebricks.bid` 上跑的是旧的 Cloudflare Worker（已 1101 报错）。
> 请到 Cloudflare **Workers** 里删除那个旧 Worker，再把 `timebricks.bid` 作为自定义域绑定到本 Pages 项目。

---

## 三、邮件（邮箱验证）— 真实收验证码的关键

> ⚠️ **重要**：只有在本节配置了正确的 `SMTP_*` 环境变量后，注册验证码才会**真实发到用户邮箱**。
> 不配置时后端处于「开发模式」：验证码只打印到 Render 日志、并作为 `dev_code` 在接口返回，
> 页面上会提示「后端未配置 SMTP 发信」。这是为了防止你遇到「收不到邮件」的根本原因。

### 1) 在 Render 配置环境变量（以 QQ 邮箱为例）
Render 控制台 → 你的 Web Service → **Environment** → 添加：
- `SMTP_HOST` = `smtp.qq.com`
- `SMTP_PORT` = `465`
- `SMTP_USER` = `你的邮箱@qq.com`
- `SMTP_PASS` = **授权码**（注意：**不是邮箱登录密码**，要去邮箱设置里生成「授权码」）
- `SMTP_FROM` = `你的邮箱@qq.com`（可不填，默认同 SMTP_USER）
- 不填 `SMTP_TLS` 时默认按 465 走 SSL；其它端口（如 587）会自动 STARTTLS。

### 2) 其它常见邮箱
| 服务商 | SMTP_HOST | 端口 | 备注 |
|--------|-----------|------|------|
| QQ 邮箱 | `smtp.qq.com` | 465 | 用**授权码** |
| 163 邮箱 | `smtp.163.com` | 465 | 用**授权码** |
| Gmail | `smtp.gmail.com` | 465 | 用「应用专用密码」(App Password) |
| 阿里云邮件推送 / SendGrid | 见各自控制台 | 465/587 | 用 API Key 或 SMTP 凭据 |

### 3) 改完变量后**必须重新 Deploy 一次**
改环境变量不会自动重新部署。Render 控制台 → 你的服务 → **Manual Deploy → Deploy latest commit**，
让新配置在进程启动时加载（启动时会 `print` 是否进入开发模式，可在日志里确认）。

### 4) 自查清单（收不到邮件时）
- [ ] `SMTP_HOST` 是否已设置（留空 = 开发模式，必然不发信）
- [ ] `SMTP_PASS` 是**授权码/应用专用密码**，不是登录密码
- [ ] 是否触发了一次新的 Deploy（环境变量已生效）
- [ ] 注册后页面提示是「验证码已发送至 …」还是「未配置 SMTP」
- [ ] Render 日志里是否出现 `[MAIL-ERROR] ...`（一般是账号/密码/端口问题）

---

## 四、本地开发

```bash
cd trading_tool
pip install -r requirements.txt
export DATABASE_PATH=./dev.db
# 可选：export SMTP_HOST=... 等
uvicorn web_app:app --reload --port 8000

# 另开终端起一个静态服务预览前端（默认指向上面的本地后端）
cd ../frontend
python3 -m http.server 5500
# 浏览器打开 http://localhost:5500
```

---

## 五、新增能力速览

| 能力 | 说明 |
|------|------|
| 用户系统 | `/api/auth/register` `/verify` `/login` `/resend` `/me`，SQLite 存账号，pbkdf2 哈希，会话令牌 |
| 自选看板（按用户） | 登录后 `/api/watchlist/add` `remove` 增删；未登录回退到默认看板 |
| 每日行情落库 | 每次行情/回测请求顺手写入 `daily_data`（按 symbol+日期），容错时可回退 |
| 容错 | 实时行情拉取失败时，自动回退到本地已存每日数据并标记 `stale` |
| CORS | 后端已放开跨域，允许 Pages 前端调用 |

## 六、管理员后台 · EDM 群发 · 客服工单

### 1) 管理员身份
- **首个注册用户自动成为管理员**（库内 `is_admin=1`）。
- 也可在 Render 环境变量 `ADMIN_EMAILS` 配置逗号分隔的邮箱，命中即管理员。
- 登录后页面顶部出现「管理」标签（仅管理员可见）。

### 2) SMTP 真实发信（后台可配置）
除了在 Render 配 `SMTP_*` 环境变量外，管理员可在 **管理 → SMTP 配置** 里直接填写并保存，
配置写入数据库 `settings` 表（优先级高于环境变量），保存后会**自动发一封测试邮件**验证连通性。
配置项：`smtp_host / smtp_port / smtp_user / smtp_pass(授权码) / smtp_from / smtp_tls`。
> 验证码、EDM、工单通知都走这套配置；未配置则仍是开发模式（验证码打印到日志 + 返回 dev_code）。

### 3) 用户管理与 EDM 统计
- `GET /api/admin/stats`：注册总数 / 已验证数 / 近 7 天 / 近 30 天 / 未结工单数。
- `GET /api/admin/users`：注册用户列表（脱敏，不含密码）。
- `POST /api/admin/edm/send`：向全部或仅已验证用户**群发邮件**（新特性通知等）。
  适合「有新特性发布时一键通知所有客户」。

### 4) 客服咨询 + 自动工单
- 页面右下角常驻「💬 客服咨询」按钮，用户填写 **称呼 / 邮箱 / 国家 / 问题** 提交。
- `POST /api/contact`（无需登录）会把咨询写入 `tickets` 表，并**立即发邮件到 `SUPPORT_EMAIL`（默认 support@timebricks.bid）**，即自动建单。
- 管理员在「管理 → 客服工单」中查看、回复；回复会**邮件通知客户**。

### 环境变量（新增）
| 变量 | 默认 | 说明 |
|------|------|------|
| `SUPPORT_EMAIL` | `support@timebricks.bid` | 客服工单收件邮箱 |
| `ADMIN_EMAILS` | 空 | 额外管理员邮箱（逗号分隔） |
| `SITE_NAME` | `Autopilot 投资分析` | 邮件标题前缀 |
| 自选看板（按用户） | 登录后 `/api/watchlist/add` `remove` 增删；未登录回退到默认看板 |
| 每日行情落库 | 每次行情/回测请求顺手写入 `daily_data`（按 symbol+日期），容错时可回退 |
| 容错 | 实时行情拉取失败时，自动回退到本地已存每日数据并标记 `stale` |
| CORS | 后端已放开跨域，允许 Pages 前端调用 |
