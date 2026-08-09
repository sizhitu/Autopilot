# 订阅落地（Polar · 个人卖家 · $9.9/月）

适合：**未注册公司**、只有新加坡 OCBC / Wise 等个人账户的情况。

## 资金链路（推荐）

```
用户信用卡
  → Polar（Merchant of Record，代收税与卡支付）
  → 结算到 Wise
  → 需要时转到 OCBC
```

不要：让用户直接转账到 N26 / Fiat24 / 个人卡。

## 一步步开通

### 1. Polar 账号

1. 打开 https://polar.sh 用个人邮箱注册  
2. 按提示完成 KYC（个人 / Individual）  
3. 税务表单按提示填写（非美国居民常见为 W-8BEN）  
4. **Payout**：绑定 **Wise**（优先）或你能稳定收 USD/EUR 的账户  

### 2. 创建产品

1. Dashboard → Products → 新建  
2. 名称例如：`Autopilot Pro`  
3. 类型：Subscription，周期 **Monthly**，价格 **$9.90 USD**  
4. 保存后，点产品右侧菜单 → **Copy Product ID** → 记到 `POLAR_PRODUCT_ID`

### 3. Access Token

1. 组织设置里创建 **Organization Access Token**（或 Personal Access Token，需含 checkouts 权限）  
2. 记到环境变量 `POLAR_ACCESS_TOKEN`

### 4. Webhook

1. Polar → Settings → Webhooks → Add endpoint  
2. URL：`https://你的后端域名/api/billing/webhook/polar`  
   （也兼容 `/api/billing/webhook`）  
3. 至少勾选：  
   - `order.paid`  
   - `subscription.created`  
   - `subscription.active`  
   - `subscription.updated`  
   - `subscription.canceled`  
   - `subscription.revoked`  
4. 复制 Signing secret → `POLAR_WEBHOOK_SECRET`

### 5. 数据库（老用户终身免费）

在 Supabase SQL Editor 执行：

`supabase/migrations/0003_subscription.sql`

执行后，**当时已存在的用户**会被标为 `plan=lifetime`，之后新注册默认为 `free`。

### 6. 后端环境变量（Render 等）

```text
POLAR_ACCESS_TOKEN=polar_oat_...
POLAR_PRODUCT_ID=...
POLAR_WEBHOOK_SECRET=polar_whs_...
BILLING_SUCCESS_URL=https://你的前端域名/?billing=success
BILLING_REQUIRED=0
```

先保持 `BILLING_REQUIRED=0`，自测支付成功后用户变为 `pro`，再改为 `1` 打开门禁。

### 7. 自测

1. 用一个**新注册**测试号登录  
2. 点「订阅 $9.9/月」→ 跳转 Polar 结账（可用 Polar 沙盒）  
3. 支付成功后刷新，`/api/auth/me` 应显示 `plan: pro`  
4. 老账号应仍为 `lifetime`，无需付费  

## 可选：只用不需 Token 的固定链接

若暂时拿不到 API Token，可在 Polar 后台生成 **Checkout Link**，填：

```text
POLAR_CHECKOUT_LINK=https://buy.polar.sh/polar_cl_xxx
```

仍需配置 Webhook，才能把付款用户写成 `pro`。有 Token 后建议改用 API（能绑定 `external_customer_id`，对账更准）。

## 注意

- 本说明不构成法律/税务建议；新加坡税务居民请自行确认申报义务。  
- `lifetime` / `grandfather` 用户不会因订阅取消被降为 free。  
- N26、Fiat24 不建议作为 Polar 结算账户。
