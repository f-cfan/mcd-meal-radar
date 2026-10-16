# 麦当劳官方 MCP 实测踩坑全记录（10 个）

> 这不是文档摘抄，是 2026-10-15 ~ 10-16 两天**真实调用**撞出来的。
> 官方文档没写的部分，本文全部标了实测日期和真实返回。
>
> **如果你正在对接任何 MCP（不限于麦当劳），第 3 和第 7 条建议先看 ——
> 它们会静默失败，不报错，你只会以为是自己代码写错了。**

---

## 先看这个：一半的坑都表现为「成功但数据是空的」

MCP 最恶心的地方不是报错，而是**返回一个形状完全正常、内容却是空的响应**。
你不会怀疑 MCP，你会怀疑自己的代码，然后花两天调自己的 bug。

| 现象 | 你以为的原因 | 真实原因 |
|---|---|---|
| `success: true`，`data` 为空 | 参数名写错了 | 参数**没传**（第 7 条） |
| `data.price` 是 `0` | 商品免费 | 漏传 `beCode`，价格没算（第 5 条） |
| 营养数据全是 `null` | 官方没数据 | 返回的是 DSL 字符串，你当 JSON 解析失败（第 2 条） |
| 推荐用户**用不了的券** | 券过期了 | 券有适用商品范围，你没读（第 8 条） |
| 热量显示 221 kcal（无糖可乐实际 0） | 官方数据错 | 编码在两个接口里指向不同商品（第 9 条） |

---

## 1. 响应是「文本说明 + 原始 JSON」双层结构

**实测**：2026-10-15

```
content[0].text = "# 您的优惠券列表\n\n共 6 张可用优惠券 | 第 1/1 页...\n\n<img src=...>"
content[1].text = {"success":true,"code":200,"data":[...]}
```

`content` 是数组，**说明文字和 JSON 分装在不同元素里**，而且 JSON 那条
还可能是**字符串**而非对象。

**后果**：直接 `json.loads(content[0].text)` 会失败；不加判断就整条链路拿不到数据。

**怎么防**：

```python
def unwrap(payload: dict) -> dict:
    """把双层响应拆成干净的业务对象。"""
    texts = [c.get("text", "") for c in payload.get("content") or []]
    for t in texts:
        t = (t or "").strip()
        if not t.startswith("{"):
            continue
        try:
            inner = json.loads(t)          # 可能是 str，要 try
        except ValueError:
            continue
        if isinstance(inner, str):        # 双层字符串
            try:
                inner = json.loads(inner)
            except ValueError:
                continue
        if isinstance(inner, dict) and "data" in inner:
            return inner
    return {}
```

---

## 2. `list-nutrition-foods` 返回 DSL 字符串，不是 JSON

**实测**：2026-10-15

```
[160]{productName,nutritionDescription,energyKj,energyKcal,protein,
 fat,carbohydrate,sodium,calcium}:
  猪柳麦满分,null,1288,308,16,16,24,781,213
  ...
```

表头带 `{...}`，行用逗号分隔，且**表头无编码**（UTF-8 也不带 BOM）。

**后果**：想当然 `json.loads` 直接崩；就算硬解也只有名字没有编码。

**怎么防**：按行 split，注意**表头声明的列数要跟实际对齐**——
有的行 `nutritionDescription` 是 `null`，有的行可能整段缺失。

```python
header_end = dsl.index("}:") + 2
header = dsl[:header_end]
cols = header[header.index("{") + 1:header.index("}")].split(",")
for line in dsl[header_end:].strip().splitlines():
    vals = line.split(",")
    row = dict(zip(cols, vals))          # 关键：zip 会自动截断，不足补空
```

⚠️ **别把缺失字段填 0**。「这个菜不含钙」和「官方没提供钙」是两回事，
填 0 是在骗用户。

---

## 3. 营养表只覆盖单品，菜单以套餐为主 → 命中率仅 22%

**实测**：2026-10-15，营养表 158 项标准单品 vs 门店菜单 134 项（大量是「X四件套」）

两张表的**交集极小**：

| 表 | 命名体系 | 例 |
|---|---|---|
| `list-nutrition-foods` | 标准单品 | 中薯条 / 麦乐鸡5块 / 猪柳蛋麦满分 |
| `query-meals` | 套餐与联名 | 龙焰鸡腿堡四件套 / 蓝莓爆爆珠麦旋风 |

**后果**：直接按名字 join，命中率 **22%（29/134）**。
「控热量」这个功能在真实推荐面前基本被架空。

**怎么防**：用官方自己的 `query-meal-detail` 拆套餐，再联表相加：

```
query-meal-detail 返回 data.rounds[]，每轮有 choices[]，
其中 isDefault == 1 的那一项就是套餐默认内容
```

```python
for rd in detail.get("rounds") or []:
    default = next((c for c in rd.get("choices") or []
                    if c.get("isDefault") == 1), None)
    if default:
        parts.append(default["name"])      # 这就是套餐由哪几样组成
```

联表相加后命中率 **75%+**，且**全部来自官方数据，不含任何估算**。

⚠️ 有子项在营养表里对不上时，记入 `unmatched` 并**如实显示「部分子项营养未提供」**，
不要拿相近品类的数据填坑。

---

## 4. 活动同一内容同时出现在「往期回顾」与「今日」段

**实测**：2026-10-15

`campaign-calendar` 返回里，同一个活动标题会在「往期回顾」和「今日推荐」
两个区段各出现一次。

**后果**：只用「标题是否出现在往期回顾里」判断进行中，会**误杀正在进行的活动**。

**怎么防**：判断进行中要看**状态字段**，不能只看出现在哪个区段。

---

## 5. 外送场景漏传 `beCode` → 成功但价格为 0

**实测**：2026-10-15

```json
// 传了 beCode
{"success":true,"data":{"price":3350,"productPrice":3350}}

// 漏传 beCode
{"success":true,"data":{}}        // ← 也是 success:true！
```

**后果**：这是最典型的**静默失败**。`success` 是 true，不抛异常，
你只会看到「怎么价格是 0」。

**怎么防**：每次调用后**校验关键字段非空**才写入计价表：

```python
data = payload.get("data") or {}
if not data or data.get("price") is None:
    continue          # 没拿到价就不记，绝不用 0 顶替
```

---

## 6. 实付含配送与打包费，只看商品价会以为算错

**实测**：2026-10-15

一份 ¥19.70 的单品外送：

```
price        = 2670   ← ¥26.70（这是用户实付）
productPrice = 1970   ← ¥19.70（只是商品）
deliveryPrice=  600
packingPrice =  190
```

**后果**：拿 `productPrice` 跟推荐逻辑里的价格比较，会发现对不上，
以为是 bug。真实原因是**外送场景的 `price` 含附加费**。

**怎么防**：费用构成必须一起返回给用户看，否则他一定觉得算错了：

> 实付构成：商品 ¥19.70 + 配送 ¥6.00 + 打包 ¥1.90

---

## 7. 🔴 最阴的一个：漏传 `beType` 伪装成「商品售罄」

**实测**：2026-10-16

```
{"code":"...","storeCode":"...","orderType":2,"items":[...]}   // 没传 beType

→ "beType1-到店取餐，2-麦乐送到家，5-得来速(DT)，6-企业团餐"
```

官方**不回「缺少参数」**，而是回一句 `beType` 的枚举取值说明。
第一眼看上去像在「告诉你参数怎么填」，实际上是在说「你根本没传」。

**真正的麻烦**：它和「商品已售罄」的响应几乎一样：

| 响应 | `data` | `message` |
|---|---|---|
| 参数缺失 | 空 | `beType1-到店取餐，2-麦乐送到家...`（枚举说明） |
| 商品售罄 | 空 | `商品已售罄或不可售，换一个商品或检查orderType/beType` |

⚠️ **两者无法靠响应内容区分**，只能靠「入参是否完整」预判。
也就是说：**一旦你发出去了，就再也分不清是哪种情况了。**

**怎么防**：把场景矩阵收敛成唯一入口，缺项**直接抛错、不发请求**。

```python
SCENES = {
    "pickup":      SceneSpec(order_type=1, be_type=1, has_be_code=False, needs_take_way=True),
    "drive_thru":  SceneSpec(order_type=1, be_type=5, has_be_code=True,  needs_take_way=True),
    "delivery":    SceneSpec(order_type=2, be_type=2, has_be_code=True,  needs_address=True),
    "group":       SceneSpec(order_type=2, be_type=6, has_be_code=True,  needs_address=True),
}

def build_price_args(scene, store_code, items, **kw):
    spec = SCENES.get(scene)
    if spec is None:
        raise MCPError(f"未知就餐场景：{scene}")
    args = {"storeCode": store_code, "orderType": spec.order_type,
            "beType": spec.be_type, "items": items}
    if spec.has_be_code and not kw.get("be_code"):
        raise MCPError(f"{spec.label}场景必传 beCode，缺失会静默返回空价格")
    if spec.needs_take_way and not kw.get("take_way_code"):
        raise MCPError(f"{spec.label}场景必传 takeWayCode")
    if spec.needs_address and not kw.get("address_id"):
        raise MCPError(f"{spec.label}场景必传 addressId")
    return args
```

**关键设计**：**宁可标「未核价」，也不发一个注定失败的请求。**
发出去之后你分不清是参数错还是商品下架，只能靠猜。

> 我自己的教训：这个 bug 让我在示例邮件里连续两天出现「未核价」，
> 而我一直在查「是不是价格表覆盖不够」—— 真因是这个。
> 一个现象可能有三层原因，而我在最表面那层找了很久。

---

## 8. 🔴 券有「适用商品范围」，不校验就会推荐用户用不了的券

**实测**：2026-10-16

`query-store-coupons` 的**每张券**都带 `products`：

```json
{"title": "9.9元中杯冰美式",
 "couponId": "264B97DBE02BE6CF996AA6914151C4A9",
 "products": [{"productCode": "507411", "productName": "麦咖啡™冰美式中杯"}]}
```

⚠️ 附带坑：这个接口的 `data` 是**数组**，不是 `{coupons:[...]}`。
只认对象结构的话，线上门店券会**恒为空**，整个券功能静默降级。

**后果**（两层）：

1. 忽略 `products` → 造出「无糖饮品券 + 麦辣鸡腿汉堡」这种组合，
   `calculate-price` 核不出价 → 显示「未核价」
2. **更糟**：它排在推荐首位，而用户下单时根本用不了这张券

**实测最刺眼的一幕**：当前门店 3 张券的适用商品
（`9900012387` / `9900016371` / `507411`）**全都不在 123 项菜单里**。
不校验适用范围，就会持续推荐无法使用的券。

**怎么防**（判定规则要保守）：

```python
if coupon.applies_to and not (cand_codes & set(coupon.applies_to)):
    continue          # 官方给了适用范围，必须命中才配券
# 官方没给 applies_to → 不做推测，沿用旧行为（宁可漏也不错配）
```

⚠️ **「官方没给范围」不能推定为「全部适用」也不能推定为「都不适用」**。
这两种推定都会产生错误推荐。

---

## 9. 🔴 同一编码在两个接口里可能指向不同商品

**实测**：2026-10-16

```
query-meals      9900008752 →「无糖可乐(中)」
query-meal-detail 9900008752 →「派」，默认项「菠萝派」
```

**后果**：如果不校验名称就联表，会把**菠萝派的营养**
（221 kcal / 147 mg 钠）挂到无糖可乐头上 —— 而官方营养表里
无糖可口可乐中杯是 **0 kcal**。差一个数量级，直接误导用户控钠控热量。

**怎么防**：详情名与菜单名不一致时**跳过**：

```python
detail_name = payload.get("data", {}).get("name")
if detail_name and detail_name != menu_item.name:
    print(f"⚠️ 营养跳过 {menu_item.name}：官方详情名「{detail_name}」与菜单名不一致")
    continue        # 宁可显示「未核实」，也不能挂上别人的数据
```

---

## 10. 名称归一化丢掉规格 → 三个规格共用一份营养

**实测**：2026-10-16

官方两张表叫法不一致：

| 菜单 | 营养表 |
|---|---|
| `可乐(中)` | `可乐中杯` |
| `无糖可乐(中)` | `无糖可口可乐中杯` |
| `薯条(小)` | `小薯条` |

早期归一化直接把括号删掉，于是 `可乐(小)/可乐(中)/可乐(大)` 全变成 `可乐`，
**三档共用同一份营养** —— 而官方实测是 **107 / 147 / 224 kcal**。

**后果**：给中杯用户报了小杯的热量，而且完全看不出异常。

**怎么防**：**先抠出括号里的规格，删括号后再按品类补回正确量词**。

```python
spec = ""
m = re.search(r"[（(]\s*([大小中])\s*[）)]", s)
if m:
    spec = m.group(1)                    # ← 关键：先保存规格
s = re.sub(r"[（(][^）)]*[）)]", "", s)   # 再删括号
s = s.replace("无糖可口可乐", "无糖可乐")  # 官方固定叫法的确定性改写
s = re.sub(r"\s+", "", s)
if spec:
    if re.search(r"(可乐|咖啡|奶茶|红茶|绿茶)([大小中])?$", s):
        s = f"{s}{spec}杯"                 # 饮品补「杯」
    elif re.search(r"薯条$", s):
        s = f"{spec}薯条"                 # 薯条：规格在前、无量词
```

⚠️ 这类改写必须是**确定性等价**，不是模糊匹配。
不确定的宁可匹配不上，保持 `None` —— 显示「营养未核实」比报错数据诚实。

---

## 附：还有 3 个不算坑但会咬人的细节

### `query-meal-detail` 的参数名是 `code`，不是 `productCode`

```json
// ❌ {"productCode":"9900005456", ...}  → {"success":false,"message":"缺少参数"}
// ✅ {"code":"9900005456", "storeCode":"...","beType":2,"orderType":2,"beCode":"..."}
```

### 错误信息在 `result` 顶层，不在 `content` 里

出错时 `content[].text` 是**空字符串**，真正的错误在同级：

```json
{"success": false, "code": 400, "message": "缺少参数", "traceId": "..."}
```

只读 `content` 会误判成「接口无响应」。

### `expiresInDays: 0` 会被 Python 的 `or` 吞掉

```python
# ❌ 0 是 falsy，"今天到期"会变成 None
days = raw.get("expiresInDays") or raw.get("validDays")

# ✅
days = next((v for v in (raw.get("expiresInDays"), raw.get("validDays"))
             if v is not None), None)
```

这个 bug 的隐蔽之处：它不会报错，只会让「今天到期」的券**永远不触发提醒**。

---

## 这 10 个坑的共同点

不是「官方文档写得烂」，而是**MCP 这类接口的失败模式本身**：

1. **静默多于报错** —— 返回 `success: true` + 空 data 是常态，不是异常
2. **失败原因不可从响应区分** —— 参数缺失和业务失败长得一样（第 7 条）
3. **字段比你以为的少** —— 官方给了 `products` 但你可能没读（第 8 条）
4. **两张表口径不一致** —— 名字、编码、规格都可能对不上（第 9、10 条）

**所以对接 MCP 的通用原则是**：

> 把「参数完整」和「响应有效」两道关**都在请求发出前**做完。
> 发出去的请求如果失败，你往往拿不到足够信息判断原因。

---

## 项目

这些坑全部来自 [麦麦饭点雷达](https://github.com/f-cfan/mcd-meal-radar) —— 一个
基于麦当劳官方 MCP 的定时优惠与营养提醒助手，221 个单测，
`fixtures/` 里有 9 份真实响应录制。

它的定价只有唯一来源：`calculate-price` 的真实返回。
算不出来就写「未核价」，而且**未核价的候选根本不进推荐位** ——
给一个无法执行的推荐，价值是零。

MIT 协议，欢迎 star 和提 issue。你大概率也会踩到第 7 条。
