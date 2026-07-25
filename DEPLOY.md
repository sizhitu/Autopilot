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

## 三、邮件（邮箱验证）

不配 SMTP 也能跑：注册时验证码会**打印到 Render 日志**，并作为 `dev_code` 在接口返回，方便本地/初次调试。

要真实发信，在 Render 的 Environment 配置（以 QQ 邮箱为例）：
- `SMTP_HOST` = `smtp.qq.com`
- `SMTP_PORT` = `465`
- `SMTP_USER` = `你的邮箱@qq.com`
- `SMTP_PASS` = **授权码**（非登录密码）
- `SMTP_FROM` = `你的邮箱@qq.com`

改完环境变量后，**手动触发一次新的 Deploy** 使配置生效。

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
