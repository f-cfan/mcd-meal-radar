# MCP_INTEGRATION.md

本项目实际使用的麦当劳 MCP Server、Tool、调用流程与业务价值。

- **接入地址**：`https://mcp.mcd.cn`
- **传输协议**：Streamable HTTP（不支持 WebSocket）
- **鉴权**：`Authorization: Bearer YOUR_MCP_TOKEN`
- **参考版本**：官方 README v1.0.9（2026-09-10）
- **数据声明**：本项目所有金额、门店、营养、活动信息均来自 MCP 真实返回，不使用模型估算、不使用静态示例冒充实时数据。

---

## 一、Tool 使用清单

本项目仅调用 **7 个只读 Tool** 与 **1 个领券 Tool**，**不调用任何下单类 Tool**。

### 使用中（8 个）

| Tool | 用途 | 调用档位 |
|---|---|---|
| `delivery-query-stores` | 查询可配送门店，确定 storeCode | 全档 |
| `query-meals` | 查询当前门店可售菜单 | 全档 |
| `list-nutrition-foods` | 餐品营养成分（热量/蛋白/钠等） | 全档 |
| `query-store-coupons` | 当前门店可用券（会校验门店） | 全档 |
| `query-my-coupons` | 卡包全量券（不校验门店） | 全档 |
| `calculate-price` | 对候选组合真实计价 | 全档 |
| `campaign-calendar` | 当月活动日历 | 全档 |
| `auto-bind-coupons` | 自动领取当前全部可用券 | 每日 09:00 |

### 已实现但默认关闭（保留能力，需显式开启）

| Tool | 说明 | 默认状态 |
|---|---|---|
| `query-my-account` | 积分余额，用于「即将过期积分」提醒 | 关闭 |
| `query-nearby-stores` | 到店场景的附近门店 | 关闭 |
| `order-list` | 历史订单，用于首次构建偏好画像 | 关闭 |

### 明确不使用（安全边界）

| Tool | 不使用的原因 |
|---|---|
| `create-order` | 创建订单。**推荐归推荐，下单权始终在人** |
| `cancel-order` | 不代用户取消订单 |
| `mall-create-order` | 涉及积分扣减与库存扣减 |
| `party-order-create` | 涉及活动名额占用 |
| `draw-lottery` | 消耗积分，不可逆 |
| `query-order` / `mall-order-detail` | 仅在用户主动要求查单时使用 |

> **为什么不用 `create-order`**
> 官方 MCP 的 33 个 Tool 中**没有任何支付工具**。`create-order` 的返回是「订单详情与**支付链接**」，而非付款码。
> 这是官方刻意的设计边界 —— 支付环节保留人工确认，责任划分清晰。
> 本项目尊重这一边界：只把决策做到「就差你确认」，不代下单、不接触资金。

---

## 二、调用流程

### 主链路（午间 / 晚间建议）

```
① delivery-query-stores   → storeCode（可配送门店列表）
      ↓
② query-meals             → data.categories（分类 + 编码 + 标签）
                            data.meals（编码 → 名称 + 价格）  ⚠️ 两处必须合并
      ↓
③ list-nutrition-foods    → { code: Nutrition }（缺失字段为 None）
      ↓
④ query-store-coupons     → 当前门店券（优先使用）
   query-my-coupons       → 卡包券（门店券缺失时兜底）
      ↓
⑤ 枚举候选组合（本地，不调 MCP）
      ↓
⑥ calculate-price         → 对每个候选组合取真实实付金额
      ↓
⑦ campaign-calendar       → 当月活动（仅作提示，不参与排序）
      ↓
⑧ 四层约束求解 → 邮件推送
```

### 自动领券链路（每日 09:00）

```
auto-bind-coupons → 自动领取当前全部可用券
      ↓
query-store-coupons → 确认领券结果
```

### Token 失效降级链路

```
任一 Tool 返回 401
      ↓
捕获 TokenExpired（不抛给用户）
      ↓
读取 data/last_snapshot.json 缓存
      ↓
发送「需要重新授权」邮件 + 缓存快照摘要
```

---

## 三、实测踩坑记录（重要）

以下是开发过程中**实际遇到**的问题，记录下来供后续开发者参考。

### 坑 1：两个 Tool 的金额单位不一致 ⚠️ 最严重

| Tool | 字段 | 单位 | 示例 |
|---|---|---|---|
| `calculate-price` | `price` / `discount` / `subtotal` | **分** | `3350` = 33.50 元 |
| `query-meals` | `currentPrice` / `originalPrice` | **元（字符串）** | `"33.5"` |

**混用会导致金额差 100 倍。** 官方未在文档中明确标注，踩中概率很高。

**本项目处理**：`src/mcd_adapter.py` 定义 `Money` 类型，入口统一换算：

```python
cents_to_yuan(3350)         # → Money(33.50)  ← calculate-price
yuan_str_to_money("33.5")   # → Money(33.5)   ← query-meals
```

业务层永远不接触「分」。单测 `TestUnitTrap` 有防回归断言。

### 坑 2：`query-meals` 的响应需要两处合并

名称和价格**不在** `categories` 里，而在 `data.meals` 这个字典中：

```json
{
  "categories": [
    { "name": "鸡肉汉堡/卷",
      "meals": [ { "code": "9900005456", "tags": ["套餐"] } ] }   // ← 只有编码和标签
  ],
  "meals": {
    "9900005456": { "name": "麦辣鸡腿汉堡三件套",
                    "currentPrice": "33.5", "originalPrice": "47" }  // ← 名称和价格在这里
  }
}
```

只遍历 `categories` 会拿到**没有名称和价格的空壳**。必须与 `data.meals` 合并。

### 坑 3：就餐场景参数矩阵不同

| 场景 | `orderType` | `beCode` | 额外必填 |
|---|---|---|---|
| 到店自取 | 1 | **无** | `takeWayCode` |
| 得来速 | 1 | 有 | `takeWayCode` |
| 麦乐送外送 | 2 | 有 | `addressId` |
| 团餐 | 2 | 有 | `addressId` |

**漏传必填项会发出必然失败的请求。** 本项目在 `SCENES` 中显式建模，缺项时直接抛错而非发出无效请求。

### 坑 4：`takeWayCode` 的唯一来源是 `calculate-price`

不能自己写死"堂食=1"这类映射。官方在 `calculate-price` 返回的 `takeWayList` 里给出 `code`：

```json
"takeWayList": [
  { "code": "eat-in",   "title": "堂食", "subtitle": "店内用餐" },
  { "code": "locker-in", "title": "外带", "subtitle": "店内取餐柜" }
]
```

创建订单时必须原样回传这个 `code`。

### 坑 5：特调传参需要成对出现

含 `unselectedKey` 的特调组（如汉堡换胚、饮品换规格），**选中项传 `selectedKey`、未选项传 `unselectedKey`，两者都要传**给 `calculate-price` / `create-order`，只传一个会被判为参数不完整。

### 坑 6：`calculate-price` 不接 couponId

券的抵扣**不通过参数传入**，而是体现在返回的 `discount` 字段与 `couponList` 中。
因此同一个商品组合在「无券」与「有券」时可能对应不同的计价签名，不能凭商品组合反推券的效果。

### 坑 7：券的两种来源语义不同，不可混用

| Tool | 语义 | 风险 |
|---|---|---|
| `query-my-coupons` | 卡包全量，**不校验门店** | 券可能到店用不了 |
| `query-store-coupons` | **仅当前门店**可用 | 范围窄但真实可用 |

**本项目策略：优先用门店券，仅在无门店券时用卡包券兜底。** 混用会产生无法核价的组合。

### 坑 8：官方数据本身字段不全

`list-nutrition-foods` 返回的营养字段常缺失（如 `calcium: null`）。
**缺失必须保留为 `None`，绝不能用 `0` 冒充「不含」** —— 两者语义完全不同。
本项目在邮件中显示「未核实」，而不是伪造一个 0。

### 坑 9：错误码只有两个

| code | 原因 | 本项目处理 |
|---|---|---|
| 401 | Token 无效 / 过期 / 未提供 | 触发降级，发送续授权邮件 |
| 429 | 超过 600 次/分钟 | 降低频率；建议非高峰时段抓取 |

**注意**：端点在无 Token 时的实际响应为 HTTP 403 + `{"code":"0001","msg":"系统错误"}`，
与文档描述的 401 不完全一致。本项目对两者都做了 Token 失效处理。

### 坑 10：MCP Token 有效期未公开

官方 README 只说明「Token 过期需重新获取」，**未公布有效期天数**。
实测确认无 Token 时端点返回鉴权错误，说明 Token 是硬门槛。

**对定时任务的架构影响**：这是本项目最重要的设计驱动因素 ——
一次性 Skill 跑完即结束，天然不会遇到 Token 过期；定时任务每天运行，必然会遇到。
因此本项目实现了完整的降级链路（见上文「Token 失效降级链路」），
把必然发生的断点做成被设计过的用户体验，而不是静默故障。

---

## 四、业务价值

### 1. 把「被动查询」变成「主动提醒」

麦当劳 MCP 官方已有的工具能力，普遍以「用户主动提问」为前提。
本项目把它们编排成**每日四档定时任务**（09:00 / 11:00 / 16:30 / 20:00），
在不打扰的前提下完成一次信息汇总与建议。

### 2. 枚举与计价分离，杜绝模型估价

一个「帮你省钱的 AI」最容易犯的错误是**编造价格**。
本项目的做法：

```
本地枚举候选组合  →  对每个组合调 calculate-price  →  用真实返回定价
```

查不到报价的组合**留空并标注「未核价」，不用标签价推算、不让模型估算**。
这是本项目最重要的技术纪律，也是可信度的来源。

### 3. 四层约束让建议可解释

```
偏好层  你吃你的   order-list → 本地画像
钱包层  你花你的   券组合枚举 → calculate-price 真实核价
身体层  你不能垮   热量 / 钠硬上限
活动层  现在有什么   campaign-calendar（仅提示，不主导排序）
```

每条推荐都带「为什么」和「注意事项」，而不是一个不透明分数。

### 4. Token 失效从故障变成可预期的体验

定时任务必然遇到 Token 过期。本项目的设计是：
**捕获 → 立即告知 → 附缓存快照**，让用户知道发生了什么以及如何恢复，
而不是让建议静默过期。

---

## 五、复现方式

### 离线演示（零 Token）

```bash
python demo.py
```

读取 `fixtures/` 中录制的真实 MCP 响应，跑通四条链路。

### 接入真实数据

```bash
export MCD_MCP_TOKEN="你的 Token"
export SMTP_HOST="smtp.qq.com"
export SMTP_PORT=465
export SMTP_USER="你的邮箱"
export SMTP_PASSWORD="SMTP 授权码"   # 注意是授权码，不是登录密码
export RADAR_TO="收件邮箱"

python scheduler.py --live --all
```

### MCP 配置示例

见 `mcp-config.example.json`。所有敏感字段均为环境变量占位符。

---

## 六、隐私与数据处理

| 数据类型 | 处理方式 |
|---|---|
| MCP Token | 仅存环境变量 / 本地 `.env`（已 `.gitignore`），代码与日志中不出现明文 |
| SMTP 授权码 | 同上 |
| 配送地址 | 仅存于 MCP 账户侧，本项目不缓存地址明文 |
| 偏好画像（口味/忌口/常点） | 仅存本地 `data/profile.json`（已被 `.gitignore`），**不外传、不上传** |
| 邮件内容 | 仅发往用户自己配置的收件邮箱 |

本项目无第三方数据分析、无遥测上报。
