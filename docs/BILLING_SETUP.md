# 订阅落地（Waffo Pancake · $9.9/月）

收款已切换为 **Waffo Pancake**（Merchant of Record）。后端为 Python，按官方文档实现 RSA 签名调用，等价于 `@waffo/pancake-ts`。

## 环境变量

```text
WAFFO_MERCHANT_ID=MER_5qdPIwFKQZhBpJIDHfhcQ0
WAFFO_STORE_ID=STO_0toK3tfU8gSlzc3zcuK401
WAFFO_PRIVATE_KEY=-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----
# 或 CI 推荐：
# WAFFO_PRIVATE_KEY_BASE64=<整段 PEM 的 base64>

WAFFO_PRODUCT_ID=PROD_你的订阅产品ID
WAFFO_PRODUCT_TYPE=subscription
WAFFO_CURRENCY=USD

# Dashboard → 店铺 → Settings → Webhooks → Webhook Public Key
WAFFO_WEBHOOK_PUBLIC_KEY=-----BEGIN PUBLIC KEY-----\n...\n-----END PUBLIC KEY-----

BILLING_SUCCESS_URL=https://你的前端域名/?billing=success
BILLING_REQUIRED=0
```

`Environment: test` 时请使用 **Test** 环境的私钥与公钥；上线再换 Production 密钥与产品。

> 私钥不要提交到 Git。本仓库只保留变量名说明。

## 一步步开通

### 1. 商户与店铺

你已有：

- Merchant ID：`MER_5qdPIwFKQZhBpJIDHfhcQ0`
- Store ID：`STO_0toK3tfU8gSlzc3zcuK401`

在 [Pancake Dashboard](https://pancake.waffo.ai/merchant/dashboard) → API & Development 下载 **Private Key**（只显示一次）。

### 2. 创建订阅产品

Dashboard → 产品 → 新建 **Subscription**：

- 名称例如：`Autopilot Pro`
- 周期：Monthly
- 价格：`9.90` USD
- 复制 **Product ID** → `WAFFO_PRODUCT_ID`

### 3. Webhook

店铺 Settings → Webhooks → Add：

- URL：`https://你的后端域名/api/billing/webhook/waffo`  
  （也兼容 `/api/billing/webhook`）
- 至少订阅：
  - `subscription.activated`
  - `subscription.updated`
  - `subscription.canceled`
  - `subscription.uncanceled`
  - `order.completed`
- 复制 **Webhook Public Key** → `WAFFO_WEBHOOK_PUBLIC_KEY`

### 4. 自测

1. 配置环境变量后重启后端  
2. 新用户登录 → 点「订阅 $9.9/月」→ 跳转 `checkout.waffo.ai`  
3. 用 Waffo **test** 卡完成支付  
4. Webhook 到达后 `/api/auth/me` 应显示 `plan: pro`、`plan_source: waffo`  

## API 行为

| 路径 | 说明 |
|------|------|
| `POST /api/billing/checkout` | 创建 Waffo session，返回 `{ url, provider: "waffo" }` |
| `POST /api/billing/webhook/waffo` | 校验 `X-Waffo-Signature` 后更新 `profiles.plan` |

结账时写入 `orderMerchantExternalId = 用户 uid`，Webhook 凭此把订单绑回本站用户。

## 遗留 Polar / Stripe

若仍配置了 `POLAR_*` / `STRIPE_*`，仅在 **未配置齐 Waffo** 时作为回退。新部署请只配 Waffo。

## 注意

- `lifetime` / `grandfather` 用户不会因取消订阅被降为 free  
- 本说明不构成法律/税务建议  
