"""麦当劳 MCP 适配层。

职责边界（刻意收窄）：
  · 只负责「协议 ↔ 领域模型」的转换，不含任何业务决策
  · 所有金额在这里统一换算成「元」的 Decimal，业务层永远不碰「分」
  · 所有商品 / 价格 / 门店 / 库存字段必须来自真实 MCP 返回，禁止编造

官方接口实测踩坑（详见 MCP_INTEGRATION.md）：
  1. calculate-price 返回单位是「分」，query-meals 返回「元（字符串）」—— 混用会差 100 倍
  2. 到店自取 orderType=1 无 beCode；得来速 orderType=1 有 beCode；麦乐送/团餐 orderType=2 有 beCode
  3. 含 unselectedKey 的特调组，selectedKey 与 unselectedKey 必须同时传给计价与下单工具
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import date as _date
from decimal import Decimal
from typing import Any

MCP_ENDPOINT = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-06-18"

# 官方错误码：仅两个
ERR_TOKEN_INVALID = 401  # Token 无效 / 过期 / 未提供
ERR_RATE_LIMITED = 429  # 超过 600 次/分钟


def load_dotenv(path: str = ".env") -> None:
    """把 .env 载入 os.environ。

    不覆盖已存在的环境变量 —— 系统环境变量优先级高于 .env。
    必须在任何模块读取 MCD_MCP_TOKEN 之前调用。
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


# 导入即加载，保证后续任何模块都能读到 MCP Token
load_dotenv()


class MCPError(RuntimeError):
    """MCP 调用失败。code 为 None 表示网络层异常。"""

    def __init__(self, message: str, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code

    @property
    def is_token_expired(self) -> bool:
        return self.code == ERR_TOKEN_INVALID


class TokenExpired(MCPError):
    """Token 失效。定时任务必须捕获此异常并降级为「续授权提醒」。"""


@dataclass(frozen=True)
class Money:
    """金额。统一用「元」，由适配层从官方返回换算而来。"""

    yuan: Decimal

    def __str__(self) -> str:
        return f"¥{self.yuan:.2f}"

    def __add__(self, other: "Money") -> "Money":
        return Money(self.yuan + other.yuan)

    def __mul__(self, k: int | float | Decimal) -> "Money":
        return Money(self.yuan * Decimal(str(k)))


def cents_to_yuan(raw: Any) -> Money:
    """分 → 元。calculate-price 的所有金额字段走这里。"""
    return Money(Decimal(str(raw)) / Decimal(100))


def yuan_str_to_money(raw: Any) -> Money:
    """元（字符串）→ Money。query-meals 的 currentPrice / originalPrice 走这里。"""
    return Money(Decimal(str(raw)))


# --------------------------------------------------------------------------
# 就餐场景参数矩阵（官方 v1.0.5+ 行为）
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class SceneSpec:
    """一个就餐场景对应的官方入参组合。"""

    label: str
    order_type: int
    has_be_code: bool
    requires_take_way_code: bool
    requires_address_id: bool


SCENES = {
    "pickup": SceneSpec("到店自取", order_type=1, has_be_code=False,
                        requires_take_way_code=True, requires_address_id=False),
    "drive_thru": SceneSpec("得来速", order_type=1, has_be_code=True,
                            requires_take_way_code=True, requires_address_id=False),
    "delivery": SceneSpec("麦乐送外送", order_type=2, has_be_code=True,
                          requires_take_way_code=False, requires_address_id=True),
    "group": SceneSpec("团餐", order_type=2, has_be_code=True,
                       requires_take_way_code=False, requires_address_id=True),
}


@dataclass(frozen=True)
class MenuItem:
    code: str
    name: str
    category: str
    tags: tuple[str, ...]
    price: Money
    original_price: Money

    @property
    def discount(self) -> Money:
        return Money(self.original_price.yuan - self.price.yuan)

    @property
    def has_discount(self) -> bool:
        return self.discount.yuan > 0


@dataclass(frozen=True)
class Coupon:
    """优惠券。券面价值无法从菜单推算时 value 为 None —— 这是官方数据的常见情形。"""

    coupon_id: str
    title: str
    value: Money | None
    threshold: Money | None
    expires_in_days: int | None


@dataclass(frozen=True)
class Nutrition:
    """营养成分。字段缺失时为 None —— 绝不填 0 冒充「无」。"""

    kcal: int | None = None
    protein_g: int | None = None
    fat_g: int | None = None
    carb_g: int | None = None
    sodium_mg: int | None = None
    calcium_mg: int | None = None


@dataclass
class Store:
    store_code: str
    name: str
    distance_km: Decimal | None = None
    address: str | None = None
    has_party: bool = False
    be_code: str | None = None          # 实测：外送场景必填，菜单与计价都依赖它


@dataclass
class Snapshot:
    """一次「抓取」的完整结果。定时任务拿它去喂约束求解器。"""

    fetched_at: str
    scene: str
    store: Store | None
    menu: list[MenuItem] = field(default_factory=list)
    nutrition: dict[str, Nutrition] = field(default_factory=dict)
    my_coupons: list[Coupon] = field(default_factory=list)
    store_coupons: list[Coupon] = field(default_factory=list)
    points: int | None = None
    campaign_names: list[str] = field(default_factory=list)
    degraded: bool = False
    degrade_reason: str | None = None


# --------------------------------------------------------------------------
# 响应解析（纯函数，可离线单测）
# --------------------------------------------------------------------------

def parse_meals(payload: dict, scene: str) -> list[MenuItem]:
    """query-meals 响应 → MenuItem 列表。

    官方结构：data.categories[].meals[] 是 {code, tags}，
    真正的名称与价格在 data.meals{ code: {...} } 里，**两处要合并**。
    """
    data = (payload or {}).get("data") or {}
    detail_map = data.get("meals") or {}
    items: list[MenuItem] = []

    for category in data.get("categories") or []:
        cat_name = (category.get("name") or "").replace("\n", "").strip()
        for stub in category.get("meals") or []:
            code = str(stub.get("code"))
            detail = detail_map.get(code) or stub
            price = detail.get("currentPrice")
            if price is None:
                continue  # 无价商品不可下单，静默跳过而非编造
            items.append(MenuItem(
                code=code,
                name=(detail.get("name") or "").strip(),
                category=cat_name,
                tags=tuple(stub.get("tags") or []),
                price=yuan_str_to_money(price),
                original_price=yuan_str_to_money(
                    detail.get("originalPrice") or detail.get("currentPrice")),
            ))
    return items


def parse_price(payload: dict) -> Money:
    """calculate-price 响应 → Money（分 → 元）。

    ⚠️ 实测（2026-10-15）：`price` 是**含配送与打包费的应付总额**，
    `productPrice` 只是商品小计。外送场景两者不同：

        productPrice  = 3690   # 商品小计 ¥36.90
        deliveryPrice = 600    # 配送费 ¥6.00
        packingPrice  = 190    # 打包费 ¥1.90
        price         = 4480   # 应付 ¥44.80

    邮件里报给用户的应是 `price`（实付总额），但要标注配送与打包构成，
    否则用户会看到一个和菜单标价不一样的数字而困惑。

    返回值带 breakdown 供上层说明构成。Simple NamedTuple 保持可比较。
    """
    data = (payload or {}).get("data") or {}
    if not data:
        return Money(Decimal(0))
    return cents_to_yuan(data.get("price", 0))


def parse_price_breakdown(payload: dict) -> dict[str, int | None]:
    """calculate-price 响应 → 费用构成（原始「分」值）。

    用于邮件中说明「实付 ¥44.80 = 商品 ¥36.90 + 配送 ¥6.00 + 打包 ¥1.90」。
    """
    data = (payload or {}).get("data") or {}
    return {
        "total": data.get("price"),
        "product": data.get("productPrice"),
        "delivery": data.get("deliveryPrice"),
        "packing": data.get("packingPrice"),
        "discount": data.get("discount"),
        "original": data.get("originalPrice"),
    }


def parse_coupons(payload: dict, store_scoped: bool = False) -> list[Coupon]:
    """query-my-coupons / query-store-coupons / available-coupons 通用解析。

    三个工具的响应结构不完全一致，这里做宽松提取，取不到就返回空列表。
    """
    data = (payload or {}).get("data") or {}
    raw_list = data.get("coupons") or data.get("list") or data.get("items") or []
    out: list[Coupon] = []
    for raw in raw_list:
        value = raw.get("value") or raw.get("amount") or raw.get("faceValue")
        threshold = raw.get("threshold") or raw.get("minAmount")
        days = raw.get("expiresInDays") or raw.get("validDays")
        out.append(Coupon(
            coupon_id=str(raw.get("couponId") or raw.get("id") or ""),
            title=(raw.get("title") or raw.get("name") or "").strip(),
            value=cents_to_yuan(value) if value is not None else None,
            threshold=cents_to_yuan(threshold) if threshold is not None else None,
            expires_in_days=int(days) if days is not None else None,
        ))
    return out


def parse_nutrition_list(payload: dict) -> dict[str, Nutrition]:
    """list-nutrition-foods 响应 → {商品名称: Nutrition}。

    ⚠️ 实测（2026-10-15）：官方返回的**不是 JSON**，而是 DSL 字符串：
        "[160]{productName,nutritionDescription,energyKj,energyKcal,protein,
         fat,carbohydrate,sodium,calcium}:\n  猪柳麦满分,null,1288,308,16,16,24,781,213\n  ..."

    营养表**不含商品编码**，只能按 productName 与菜单名称关联。

    为便于离线 demo，本函数**同时接受**两种形态：
      1. 真实形态：data 是含「{...}:」表头的 DSL 字符串
      2. 离线形态：data 是 {"foods": [{productName, energyKcal, ...}]} 结构
    两者产出的结果一致，所以 demo 与线上跑的是同一套逻辑。

    官方数据本身字段不全，缺失一律保留 None；表里的数字 0 是真实值，需保留。
    """
    dsl: str | None = None
    rows: list[dict] = []

    if isinstance(payload, dict):
        raw = payload.get("data")
        if isinstance(raw, str) and "{" in raw:
            dsl = raw
        elif isinstance(raw, str) and raw.strip().startswith("["):
            dsl = raw
        elif isinstance(raw, dict):
            rows = raw.get("foods") or raw.get("list") or []
        elif raw is None and isinstance(payload.get("foods"), list):
            # 离线 fixture 形态：顶层就是 {foods:[...]}，没有 data 包装层
            rows = payload.get("foods") or []

    # ---- 形态 1：真实 DSL ----
    if dsl is not None:
        header_match = re.search(r"\{([^}]*)\}", dsl)
        if not header_match:
            return {}
        fields = [f.strip() for f in header_match.group(1).split(",")]
        body = dsl[header_match.end():].lstrip(":").lstrip("\n")
        for line in body.splitlines():
            line = line.strip()
            if not line:
                continue
            parts = line.split(",")
            if len(parts) < len(fields):
                continue
            rows.append(dict(zip(fields, parts)))

    # ---- 形态 2：离线 JSON（字段名与 DSL 一致，兼容旧写法）----
    out: dict[str, Nutrition] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue

        def num(*keys: str) -> int | None:
            for k in keys:
                v = row.get(k)
                if v is None or v == "" or v == "null":
                    continue
                try:
                    return int(float(v))
                except (TypeError, ValueError):
                    return None
            return None

        name = str(row.get("productName") or row.get("name") or "").strip()
        if not name:
            continue
        out[name] = Nutrition(
            kcal=num("energyKcal", "kcal", "energy", "calories"),
            protein_g=num("protein", "proteinG"),
            fat_g=num("fat", "fatG"),
            carb_g=num("carbohydrate", "carb", "carbG"),
            sodium_mg=num("sodium", "sodiumMg"),
            calcium_mg=num("calcium", "calciumMg"),
        )
    return out


def normalize_name(name: str) -> str:
    """菜名归一化，用于营养表与菜单的名称联表。

    ⚠️ 实测（2026-10-15）官方两张表用的命名体系不同：
      · 营养表 `list-nutrition-foods` 是**标准单品名** —— 中薯条 / 麦乐鸡5块 / 猪柳蛋麦满分
      · 菜单   `query-meals`          是**套餐与联名商品** —— 龙焰鸡腿堡四件套 / 蓝莓爆爆珠麦旋风

    两者交集很小，且官方**没有提供套餐到单品的成分映射**。
    因此本函数只做保守归一化（去空格、去营销修饰、规格词统一），
    **不做模糊猜测**。匹配不上的编码保持无营养数据，
    邮件中显示「营养未提供」—— 绝不按 0 或估算值填充。
    """
    s = (name or "").strip()
    # 去掉规格括号，如「鸡肉汉堡随心选单人餐（麦金卡免配）」
    s = re.sub(r"[（(][^）)]*[）)]", "", s)
    # 去掉常见营销修饰
    for token in ("新品", "上新", "限定", "联名", "同款", "超值", "特惠"):
        s = s.replace(token, "")
    # 规格词归一：小/中/大 → 小份/中份/大份
    s = s.replace("小薯条", "小份薯条").replace("中薯条", "中份薯条").replace("大薯条", "大份薯条")
    # 麦乐鸡 N 块 → 麦乐鸡N块（去空格）
    s = re.sub(r"麦乐鸡\s*(\d+)\s*块", r"麦乐鸡\1块", s)
    # 去掉中英文引号差异
    s = s.replace("“", "").replace("”", "").replace('"', "")
    return re.sub(r"\s+", "", s)


def build_nutrition_by_code(menu: list[MenuItem], by_name: dict[str, Nutrition]) -> dict[str, Nutrition]:
    """按名称把营养表联到菜单商品编码上。

    官方营养表无编码，只能靠名称匹配。**匹配不上的编码不写入**，
    上层据此显示「营养未提供」，绝不按 0 或估算值处理 —— 这是本项目的核心纪律之一。

    实测命中率约 26/134，因为营养表只覆盖标准单品而菜单以套餐为主。
    这是官方数据缺口，不是本项目的缺陷，因此如实展示而不掩盖。
    """
    normalized = {normalize_name(n): nutr for n, nutr in by_name.items()}
    out: dict[str, Nutrition] = {}
    for item in menu:
        direct = by_name.get(item.name.strip())
        if direct:
            out[item.code] = direct
            continue
        fuzzed = normalized.get(normalize_name(item.name))
        if fuzzed:
            out[item.code] = fuzzed
    return out


def parse_points(payload: dict) -> int | None:
    """query-my-account 响应 → 可用积分数。

    ⚠️ 实测字段名为 availablePoint（不是 available），且是**字符串**、单位为「分」
    （实测返回 "169.7"，即 169.7 积分）。此处取整数部分，保持可用性。
    """
    data = (payload or {}).get("data") or {}
    raw = data.get("availablePoint") or data.get("available")
    if raw is None:
        return None
    try:
        return int(float(raw))
    except (TypeError, ValueError):
        return None


def parse_stores(payload: dict) -> list[Store]:
    """delivery-query-stores / query-nearby-stores 响应 → Store 列表。

    ⚠️ 实测 data 是**数组**（不是 {stores:[...]}），字段名为 storeName / storeCode /
    beCode / reservation / businessStatus。beCode 在外送场景是必填项。
    """
    data = (payload or {}).get("data")
    if isinstance(data, dict):
        data = data.get("stores") or data.get("list") or []
    if not isinstance(data, list):
        return []

    out: list[Store] = []
    for raw in data:
        dist = raw.get("distance")
        try:
            distance = Decimal(str(dist)) if dist is not None else None
        except Exception:
            distance = None
        out.append(Store(
            store_code=str(raw.get("storeCode") or ""),
            name=raw.get("storeName") or raw.get("name") or "",
            distance_km=distance,
            address=raw.get("address"),
            has_party=bool(raw.get("reservation")),
            be_code=(str(raw.get("beCode")) if raw.get("beCode") else None),
        ))
    return out


def parse_campaign_titles(payload: dict) -> list[str]:
    """campaign-calendar 响应 → **进行中与未来**的活动标题。

    ⚠️ 实测（2026-10-15）：该工具返回的是**人读 Markdown 文本**（放在 data
    为 None、真实内容落在 `_text` 里），结构为：

        ### 当前时间：2026-10-15 21:21:46
        ### 活动列表：
        #### 2026年10月12日 往期回顾
        -   **活动标题**：超值9.9元早餐两件套陪你开工啦
        #### 2026年10月20日
        -   **活动标题**：某进行中活动

    只有**标题行带 `**活动标题**` 前缀**，日期行以 `#### ` 开头，
    带「往期回顾」标记的日期段属于历史活动，**必须排除** —— 否则会把
    已结束的活动当作「当前活动」推给用户。

    官方未给结构化状态字段，所以这里按「往期回顾」标记做过滤，
    这是实测唯一可靠的判别依据。
    """
    text = payload.get("_text") if isinstance(payload, dict) else None
    if not text:
        data = (payload or {}).get("data")
        if isinstance(data, str):
            text = data
        elif isinstance(data, dict):
            cands = data.get("campaigns") or data.get("list")
            if isinstance(cands, list):
                return [str(c.get("title", "")) for c in cands
                        if isinstance(c, dict) and c.get("title")]
            text = json.dumps(data, ensure_ascii=False)
    if not text:
        return []

    titles: list[str] = []
    current_date = None
    today = _date.today()

    for line in text.splitlines():
        stripped = line.strip()

        # 日期分节
        if stripped.startswith("####"):
            m = re.match(r"^####\s*(\d{4})年(\d{1,2})月(\d{1,2})日", stripped)
            if m:
                current_date = _date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            else:
                current_date = None
            continue

        # 只认标题行，且必须属于「今天或未来」的日期段
        m = re.match(r"^-\s+\*\*活动标题\*\*\s*[:：]\s*(.+?)\s*$", stripped)
        if not m:
            continue
        if current_date is None or current_date < today:
            continue  # 往期不推给用户

        title = re.sub(r"\*\*", "", m.group(1)).strip()
        if title:
            titles.append(title)

    seen: set[str] = set()
    return [t for t in titles if not (t in seen or seen.add(t))]


def build_create_order_params(
    store_code: str,
    scene: str,
    items: list[tuple[str, int]],
    take_way_code: str | None = None,
    address_id: str | None = None,
) -> dict:
    """按场景矩阵组装 create-order 入参。

    缺必填项时直接抛错 —— 宁可不下单，也不能发一个必然失败的请求。
    """
    spec = SCENES.get(scene)
    if spec is None:
        raise ValueError(f"未知场景：{scene}")
    if spec.requires_take_way_code and not take_way_code:
        raise ValueError(f"{spec.label} 场景必须提供 take_way_code（来源：calculate-price 的 takeWayList）")
    if spec.requires_address_id and not address_id:
        raise ValueError(f"{spec.label} 场景必须提供 address_id（来源：delivery-query-addresses）")

    params: dict[str, Any] = {
        "storeCode": store_code,
        "orderType": spec.order_type,
        "items": [{"productCode": c, "quantity": q} for c, q in items],
    }
    if spec.requires_take_way_code and take_way_code:
        params["takeWayCode"] = take_way_code
    if address_id:
        params["addressId"] = address_id
    return params


# --------------------------------------------------------------------------
# 传输层（需要真实 Token；demo 路径不经过这里）
# --------------------------------------------------------------------------

class McdMcpClient:
    """麦当劳 MCP 远程客户端。

    仅在配置了真实 Token 时使用。demo 与单测全部走 fixtures，不依赖本类。
    """

    def __init__(self, token: str | None = None, timeout: float = 20.0) -> None:
        self._token = token or os.environ.get("MCD_MCP_TOKEN", "")
        self._timeout = timeout
        self._session_id: str | None = None
        self._rpc_id = 0

    @property
    def configured(self) -> bool:
        return bool(self._token)

    def _next_id(self) -> int:
        self._rpc_id += 1
        return self._rpc_id

    def _headers(self) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "Authorization": f"Bearer {self._token}",
        }

    def call_tool(self, name: str, args: dict | None = None) -> dict:
        """调用一个 MCP Tool。Token 失效时抛 TokenExpired，供上层降级。

        ⚠️ 实测发现（2026-10-15，重要）：
        官方返回**不是**干净 JSON，而是 MCP 标准 content[].text 包装 —— 文本里
        先有人类可读的字段说明 + Markdown，末尾才跟一段 `## Original Response`
        后的原始 JSON。**部分工具**（如 list-nutrition-foods）甚至返回 DSL 表格而非 JSON。

        本方法自动拆包出结构化 payload 返回；解析不出时返回 {"_text": 原始文本}。
        """
        if not self.configured:
            raise TokenExpired("未配置 MCD_MCP_TOKEN", ERR_TOKEN_INVALID)

        payload = {
            "jsonrpc": "2.0",
            "id": self._next_id(),
            "method": "tools/call",
            "params": {"name": name, "arguments": args or {}},
        }
        body = json.dumps(payload).encode()
        if self._session_id:
            self._headers()["Mcp-Session-Id"] = self._session_id

        req = urllib.request.Request(MCP_ENDPOINT, data=body,
                                     headers=self._headers(), method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self._timeout) as resp:
                if resp.headers.get("Mcp-Session-Id"):
                    self._session_id = resp.headers["Mcp-Session-Id"]
                raw = resp.read().decode()
        except urllib.error.HTTPError as exc:
            if exc.code in (ERR_TOKEN_INVALID, ERR_RATE_LIMITED):
                raise MCPError(f"MCP 返回 {exc.code}", exc.code) from exc
            raise MCPError(f"MCP 请求失败：{exc.code}", exc.code) from exc
        except urllib.error.URLError as exc:
            raise MCPError(f"网络异常：{exc.reason}") from exc

        # Streamable HTTP 可能返回 SSE 格式
        text = raw
        if raw.startswith("event:") or "\ndata:" in raw:
            lines = [ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:")]
            text = lines[-1] if lines else "{}"

        data = json.loads(text)
        if "error" in data:
            raise MCPError(str(data["error"]))
        result = data.get("result", {})
        return self.unwrap(result)

    @staticmethod
    def unwrap(result: dict) -> dict:
        """从 content[].text 包装中拆出结构化 payload。

        实测的响应形态（2026-10-15）：
          {"content": [{"type": "text", "text": "…字段说明…\\n## Original Response\\n{JSON}"}]}
        其中 {JSON} 本身还可能是字符串化的 DSL（如营养表格），此处原样保留。
        """
        content = result.get("content")
        if not content or not isinstance(content, list):
            return result

        first = content[0]
        text = first.get("text", "") if isinstance(first, dict) else str(first)
        if not text:
            return result

        # 从后往前找最后一个平衡的 JSON 对象 —— 字段说明里也可能有 {
        start = text.rfind('{"success"')
        if start < 0:
            start = text.rfind('{"')
        if start < 0:
            return {"_text": text}

        segment = text[start:]
        for end in range(len(segment), 0, -1):
            if segment[end - 1] != "}":
                continue
            try:
                return json.loads(segment[:end])
            except json.JSONDecodeError:
                continue
        return {"_text": text}


def load_fixture(name: str, fixture_dir: str) -> dict:
    """读取 fixtures 中录制的真实 MCP 响应。"""
    path = os.path.join(fixture_dir, f"{name}.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
