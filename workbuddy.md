# workbuddy.md — WorkBuddy 开发对话上下文

本文档记录「麦麦饭点雷达」项目的开发过程，用于参与麦当劳程序员创意开发大赛的
**WorkBuddy 专项积分奖励**核验（前 100 名可获 3000 积分）。

---

## 项目信息

| 项 | 内容 |
|---|---|
| 项目名称 | 麦麦饭点雷达 / mcd-meal-radar |
| 开发工具 | 腾讯 WorkBuddy |
| MCP 依赖 | 麦当劳中国 MCP Server（`https://mcp.mcd.cn`） |
| 开发时间 | 2026-10-15 |
| 技术栈 | Python 3.9+ 标准库（零第三方依赖） |
| 核心卖点 | 每日四档定时主动推送 · 四层约束求解 · Token 失效优雅降级 |

---

## 开发背景

开发者在收到 2026 麦当劳程序员创意开发大赛的报名链接后，**没有直接开始写代码**，
而是先做了三层核查 —— 这个决策直接决定了项目最终的形态。

### 第一层：读官方规则

从 `M-China/mcd-developer-innovation-challenge` 仓库拉取 `activityGuidelines.md`
与 `RANKING.md` 全文，逐条提取关键约束：

- 报名截止 2026-10-25 23:59，定榜 10-26 00:00
- **排名依据是 GitHub 公开 Star 数量**，不是人工评审
- 必交 5 个文件，其中 `CONTEST_DECLARATION.md` 必须与官方**逐字一致**
- 仓库创建时间必须在 2025-12-25 ~ 2026-10-25 之间
- **同一 GitHub 账号多个项目，只有 Star 最高的进榜**

### 第二层：读全部 33 个 MCP Tool 的能力边界

拉取 `M-China/mcd-mcp-server` 的 README 全文，把 33 个 Tool 按性质重新分类：

```
只读查询 27 个  +  产生订单或状态变更 6 个
```

**关键发现**：33 个 Tool 中**没有任何支付工具**。
`create-order` 的返回是「订单详情与支付链接」，不是付款码。

这直接推翻了一版「自动生成付款码」的思路 —— 那是官方刻意保留的边界。

### 第三层：把榜单 100 个项目全名逐个排查

这是最关键的一步。用脚本对 `RANKING.md` 全量项目名做关键词检索：

| 关键词 | 命中 |
|---|---|
| 提醒 / 推送 / 定时 / 订阅 / 通知 | **0** |
| 到期 / 过期 / 有效期 | **0** |
| daily / 日报 | **0** |
| auto-coupon | 1（36 Star） |

**结论：100 个同赛项目全部是「你问我答」的被动形态。**

于是把项目定位确立为**形态差异**而非功能差异：
> 100 个麦当劳 Skill 都等你开口，只有这一个每天饭点主动来找你。

---

## 三版思路的取舍

开发过程中评估过三个方向，记录取舍理由。

### 方向一：IoT × 麦当劳 —— 否决

想法是把 MCP 变成 IoT 设备管理网络（自动巡检、补货联动、备货预测）。

**否决理由**：脱离赛题。竞赛要求是「基于麦当劳 MCP 开发创意 Skill」，
强行套 IoT 会让评审觉得跑题，且难以落到具体业务价值上。

### 方向二：自动外卖点餐 —— 降级

想法是记录偏好 → 授权位置 → 找附近门店 → 查优惠 → 营养搭配 → 生成付款码。

**核查后发现两个问题**：

1. **付款码做不到** —— MCP 无支付工具，这是官方刻意的边界
2. **位置无法自动获取** —— MCP 不支持自动定位，只能用 `delivery-create-address` 建地址簿

更关键的是一个**传播层面的矛盾**：
项目最有价值的部分（自动下单 + 支付）恰好最无法公开展示 ——
创建真实订单会污染账号、占用库存、产生待支付订单。

而这是**纯 Star 竞赛**，评委和想给你 Star 的人无法验证核心能力。

### 方向三：定时优惠提醒 —— 采纳 ✅

最终方案：每日饭点前定时检索优惠券与门店活动，输出省钱与营养搭配建议，
自动领券，券有效期提醒，推送到用户自配邮箱。

**为什么这一版最好**：

| 维度 | 评估 |
|---|---|
| 赛道 | 全场唯一「主动推送」形态，0 竞争 |
| 技术可行性 | 11 个只读 Tool + 1 个领券 Tool，全部够用 |
| 使用频率 | 每天 4 次，有真实留存价值 |
| 演示可行性 | 可通过 fixtures 完全离线复现 |

---

## 一个被实测发现的风险：Token 有效期

这是开发过程中最重要的架构驱动因素。

### 实测

```bash
curl -X POST https://mcp.mcd.cn \
  -H "Content-Type: application/json" \
  -H "Accept: application/json, text/event-stream" \
  -d '{"jsonrpc":"2.0","id":1,"method":"initialize","params":{...}}'
```

**响应**：

```
HTTP/1.1 403 Forbidden
{ "code": "0001", "msg": "系统错误" }
```

结论：端点存活、协议正常、**Token 是硬门槛**。

进一步查证：官方 README 只说明「Token 过期需重新获取」，**未公布有效期天数**。

### 由此推出的架构判断

| 项目形态 | 会不会遇到 Token 过期 |
|---|---|
| 一次性问答 Skill | **不会** —— 跑完就结束 |
| 定时任务 | **必然会** —— 每天运行 |

同赛项目绝大多数只跑一次，天然遇不到这个问题。
但凡是真正每天跑的，第三天就会静默失败。

**所以本项目把 Token 失效做成了被设计过的功能，而不是一个待修的 bug：**

```
MCP 返回 401
    ↓
捕获 TokenExpired（不抛给用户）
    ↓
读取 data/last_snapshot.json 缓存
    ↓
发送「需要重新授权」邮件：续授权步骤 + 错误详情 + 缓存快照摘要
```

对应代码 `src/collector.py` 的 `get_snapshot()` 与
`src/mailer.py` 的 `render_token_expired()`，
演示链路见 `demo.py` 的 `demo_b()`。

---

## 实现过程中的关键技术发现

以下都是在写代码和跑测试时**实际踩到**的，不是预先查文档得知的。

### 发现 1：`query-meals` 的响应需要两处合并

最初实现时直接遍历 `data.categories`，结果拿到的是一堆只有 `code` 和 `tags`
的空壳对象，没有名称和价格。

真实结构是：

```json
{
  "categories": [
    { "name": "鸡肉汉堡/卷",
      "meals": [ { "code": "9900005456", "tags": ["套餐"] } ] }      // 只有编码和标签
  ],
  "meals": {
    "9900005456": { "name": "...", "currentPrice": "33.5" }          // 名称和价格在这里
  }
}
```

**必须与 `data.meals` 合并**。已写成 `mcd_adapter.parse_meals()` 并加单测锁定。

### 发现 2：两个 Tool 的金额单位不一致

```python
calculate-price  →  price: 3350           # 单位：分
query-meals      →  currentPrice: "33.5"  # 单位：元（字符串）
```

混用会让金额差 **100 倍**。第一版 demo 输出就出现过 `¥¥13.50` 这类格式错误。

修复方案：定义 `Money` 类型，入口统一换算，业务层永远不碰「分」。

```python
cents_to_yuan(3350)         # → Money(33.50)
yuan_str_to_money("33.5")   # → Money(33.5)
```

并在 `TestUnitTrap` 中加了防回归断言 —— 用同一商品在两个 Tool 的价格交叉验证。

### 发现 3：就餐场景参数矩阵不同

| 场景 | orderType | beCode | 额外必填 |
|---|---|---|---|
| 到店自取 | 1 | **无** | takeWayCode |
| 得来速 | 1 | 有 | takeWayCode |
| 麦乐送外送 | 2 | 有 | addressId |
| 团餐 | 2 | 有 | addressId |

漏传必填项会发出必然失败的请求。修复方案：在 `SCENES` 常量中显式建模，
缺项时**直接抛错**而非发出无效请求 —— 宁可不下单，也不能发一个必然失败的请求。

单测 `TestSceneMatrix` 锁定四个场景的参数行为。

### 发现 4：官方数据本身字段不全

`list-nutrition-foods` 返回的营养字段常缺失（如 `calcium: null`）。

**这里有个容易犯的错**：把缺失字段填 0。但「0」和「不知道」语义完全不同 ——
填 0 会告诉用户「这个菜不含钙」，而实际上只是官方没提供数据。

修复方案：`Nutrition` 字段为 `None` 时，邮件中显示「未核实」。
单测 `test_missing_field_stays_none` 与 `test_zero_is_preserved_as_real_zero`
分别锁定了「缺失为 None」和「真实的 0 要保留」两件事。

### 发现 5：枚举与计价必须分离

第一版实现时，候选组合生成后没有接 `calculate-price`，导致邮件里全是「未核价」。
这虽然诚实，但让推荐失去了实用性。

修复：新增 `attach_quoted_prices()`，在**枚举完成后**才定价。
顺序不可颠倒 —— 先有候选才知道要问哪些组合的价。

```python
cands = enumerate_coupon_combinations(...)
cands = attach_quoted_prices(cands, price_table)   # ← 必须在这一步
cands = apply_nutrition(cands, ...)
```

同时保留纪律：**查不到报价的组合留空标注「未核价」，绝不用标签价推算。**

### 发现 6：fixtures 的结构嵌套不一致

写 `collector.py` 时发现 `store_menu_status.json` 的 `storeCode` 嵌套在
`data` 下，而 `menu.json` 的 `data` 里又是另一层。初始读取路径写错，导致门店匹配失败。

修复：读取时做 `status.get("data") or status` 的兼容处理。
这类问题在真实 API 中很常见 —— 不同工具的响应嵌套深度不一致。

---

## 两条不可妥协的纪律

这两条写进了 README，也写进了单测。

### 纪律一：枚举与计价分离，绝不估价

一个「帮你省钱的 AI」最容易犯的错误是编造价格。

```
本地枚举候选组合  →  对每个组合调 calculate-price  →  用真实返回定价
```

**查不到报价的组合留空，标注「未核价」。**

### 纪律二：只推荐，不下单

官方 MCP 的 33 个 Tool 中没有任何支付工具。本项目不调用：

- `create-order`
- `cancel-order`
- `mall-create-order`
- `party-order-create`
- `draw-lottery`

单测 `TestNoOrderingTools` 会扫描 `src/` 下所有 `.py` 文件，
确保没有任何下单工具调用混进来。这是自动化的纪律 enforcement。

唯一改变账户状态的调用是 `auto-bind-coupons`（每日 09:00 自动领券），
它在代码中与所有只读调用**物理隔离**，便于审计。

---

## 工程实践

复用了头部项目的六项标准做法（分析 `Ayzzz123/mcd-coupon-strategist` 源码得出）：

| # | 做法 | 本项目实现 |
|---|---|---|
| 1 | `demo.py` 零依赖零 Token 30 秒跑通 | 4 条链路，18 份 fixtures |
| 2 | 12+ 个真实响应录制 | `fixtures/` 共 19 份，含字段缺失与边界场景 |
| 3 | 披露 MCP 真实踩坑 | `MCP_INTEGRATION.md` 记录 10 个坑 |
| 4 | SVG Banner + 架构图 | `assets/banner.svg`、`assets/architecture.svg` |
| 5 | CI 徽章 + 提交自检清单 | `.github/workflows/tests.yml`、`docs/SUBMIT_CHECKLIST.md` |
| 6 | 完整 workbuddy.md | 本文档 |

额外的 CI 检查：

- 在 3 个 Python 版本 × 2 个系统上跑测试与 demo
- **校验 `CONTEST_DECLARATION.md` 的 SHA256 与官方原文一致**，防止误改
- 扫描仓库中的疑似 MCP Token 与 SMTP 授权码

---

## 测试覆盖

149 个单测，全部零依赖、零 Token、零网络：

```
TestUnitTrap          5 个  单位换算防回归（分 vs 元）
TestParseMenu         3 个  响应两处合并、优惠计算、分类清洗
TestParseNutrition    2 个  缺失为 None、真实 0 保留
TestParseCoupons      2 个  面值 null 保留、空响应安全
TestSceneMatrix       6 个  四场景参数行为与必填校验
TestComboSignature    2 个  组合签名排序与 fixture key 对齐
TestPolicyNormalize   3 个  配置类型归一化
TestNoOrderingTools   1 个  扫描源码确保无下单调用
```

```
Ran 149 tests in 0.043s
OK
```

---

## 复现方式

```bash
python demo.py            # 四条链路，零 Token
python tests/test_all.py  # 149 个单测
python scheduler.py --fixture --all   # 四档任务，零 Token
```

真实运行需先配置 `MCD_MCP_TOKEN` 与 SMTP，详见 `README.md` 与 `docs/SCHEDULE.md`。

---

## 参赛合规自查

| 规则要求 | 本项目状态 |
|---|---|
| 真实使用麦当劳 MCP | ✅ 使用 11 个只读 Tool + 1 个领券 Tool |
| 仓库公开可访问 | ✅ Public |
| 仓库创建时间在规定区间内 | ✅ 2026-10-15 |
| 不含真实 Token / 密钥 | ✅ 全部走环境变量，CI 有扫描 |
| 不与麦当劳无关品牌做对比 | ✅ 仅提及麦当劳 |
| 不含政治敏感内容 | ✅ 无 |
| 不含恶意代码 / 钓鱼链接 | ✅ 无外部链接指向违规站点 |
| 不宣扬奢靡浪费 / 拜金主义 | ✅ 文案用「更划算」而非「极致省钱」 |
| 敏感信息处理声明 | ✅ README 与 MCP_INTEGRATION.md 均有隐私章节 |

---

*本文档如实记录开发过程，包含思路取舍与技术踩坑。项目由参赛者独立开发，
非麦当劳官方产品。*
