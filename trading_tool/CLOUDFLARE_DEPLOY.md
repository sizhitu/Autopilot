# 部署到 Cloudflare（自定义域名 timebricks.bid）

本项目是一个 **Docker 化的 Python（FastAPI + uvicorn，端口 8000，依赖 pandas/numpy）** 应用，
前端 `index.html` 与 API 由同一个容器在同一端口提供。Cloudflare Pages / 普通 Workers 跑不了 Python，
因此采用 **Cloudflare Containers**：把现有 Docker 镜像直接跑在 Cloudflare 上，并用 `timebricks.bid` 作为自定义域名。

> 已通过 `wrangler deploy --dry-run` 本地验证：镜像可正常构建、配置合法、Worker 绑定与容器均被正确识别。

---

## 一、前置条件

| 条件 | 说明 |
|------|------|
| Node.js ≥ 18 | 用于运行 `wrangler` |
| Docker | 本地构建镜像（`wrangler deploy` 会调用 Docker） |
| **Cloudflare Workers 付费计划** | Containers 仅在 Workers Paid（$5/月起）可用，**免费计划无法部署** |
| `timebricks.bid` 已在 Cloudflare 账号中 | 即已在 dashboard 添加该域名并把 NS 改到 Cloudflare |
| 账号登录 | 运行 `wrangler login` 时用 `sizhitu00@gmail.com` 登录 |

### 若 timebricks.bid 还没接入 Cloudflare
1. 登录 Cloudflare → **添加站点** → 输入 `timebricks.bid`。
2. 按提示把域名注册商处的 **NS 记录**改成 Cloudflare 提供的两条（如 `xxx.ns.cloudflare.com`）。
3. 等待状态变为 **Active**（通常几分钟到几小时，取决于原注册商的 TTL）。
4. 之后才能用 `custom_domain = true` 把域名绑到 Worker。

---

## 二、部署步骤（在本机执行）

```bash
# 1. 进入容器目录
cd trading_tool

# 2. 安装 Worker 侧依赖（@cloudflare/containers）
npm install

# 3. 登录 Cloudflare（浏览器打开，用 sizhitu00@gmail.com 授权）
wrangler login

# 4. 构建并部署：本地构建 Docker 镜像 → 推送到 Cloudflare Registry → 上线 Worker + 容器
wrangler deploy
```

`wrangler deploy` 完成后：
- Worker `autopilot` 上线；
- 容器 `autopilot-app`（基于 `Dockerfile`）上线；
- 自定义域名 `timebricks.bid` 自动接入（Cloudflare 自动签发证书、托管 DNS）。

打开 **https://timebricks.bid** 即可访问。

---

## 三、关键配置说明（wrangler.toml）

- `[[containers]]`：`image = "./Dockerfile"` 指向本目录的 Dockerfile，部署时由本地 Docker 构建。
- `instance_type = "basic"`：容器规格，`lite`(默认)/`basic`/`standard-1`…`standard-4`。
  pandas/numpy 计算较多，建议 `basic` 起步，卡顿可上调到 `standard-1`。
- `[[durable_objects.bindings]]` + `[[migrations]]`：把容器作为 Durable Object 绑定给 Worker。
- `[[routes]]`：`pattern = "timebricks.bid"` + `custom_domain = true` 即自定义域名接入。
- `worker.js`：所有请求通过固定 session id `autopilot-main` 路由到**同一个**容器实例
  （适合个人低频工具）。如需水平扩展，可改为按用户/IP 生成 session id。

> 容器内 uvicorn 监听 8000（`Dockerfile` 中 `${PORT:-8000}`，与 `worker.js` 的 `defaultPort = 8000` 对应）。

---

## 四、更新 / 重新部署

代码改动提交后，在本机重新执行：

```bash
cd trading_tool
wrangler deploy
```

仅改前端 `index.html` 或后端 `*.py` 都会触发镜像重建与重新上线。

---

## 五、排错

| 现象 | 原因 / 处理 |
|------|------------|
| `Containers requires Workers Paid` | 需在 Cloudflare 升级到 **Workers 付费计划** |
| `Custom domain timebricks.bid is not in your account` | 域名还没加到 Cloudflare / NS 未生效，先完成"前置条件"第 4 步 |
| 首次访问很慢 / 超时 | 容器处于休眠，冷启动约数秒；`sleepAfter = "10m"` 控制休眠间隔 |
| 部署时 `docker: command not found` | 本机未装 Docker，先安装并启动 Docker 引擎 |
| 想锁定账号 | 在 `wrangler.toml` 取消注释 `account_id` 并填入 Cloudflare 账号 ID |

---

## 六、备选方案（如不想用 Containers）

若暂时不想开 Workers 付费计划，可走 **VPS / 容器平台 + Cloudflare 域名代理**：

1. 在 Railway / Render / Fly.io / 任意 VPS 上用现有 `Dockerfile` + `docker-compose.yml` 跑容器（暴露 8000）。
2. 在 Cloudflare 给 `timebricks.bid` 添加 **A/AAAA 或 CNAME 记录**指向该主机，并**开启橙色云（代理）**。
3. SSL/TLS 模式设为 `Full` 或 `Full (strict)`。

后端仍跑在 Cloudflare 之外，但域名与加速/证书由 Cloudflare 托管，免费且稳定。
