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
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any

MCP_ENDPOINT = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-06-18"

# 官方错误码：仅两个
ERR_TOKEN_INVALID = 401  # Token 无效 / 过期 / 未提供
ERR_RATE_LIMITED = 429  # 超过 600 次/分钟


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
    """calculate-price 响应 → Money（分 → 元）。"""
    data = (payload or {}).get("data") or {}
    return cents_to_yuan(data.get("price", 0))


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
    """list-nutrition-foods 响应 → {商品编码: Nutrition}。

    官方数据常缺字段。缺失一律保留 None，业务层据此显示「未核实」，
    绝不用 0 冒充「不含」。
    """
    data = (payload or {}).get("data") or {}
    rows = data.get("foods") or data.get("list") or []
    out: dict[str, Nutrition] = {}
    for row in rows:
        code = str(row.get("code") or row.get("foodCode") or "")
        if not code:
            continue

        def pick(*names: str) -> int | None:
            for n in names:
                if row.get(n) is not None:
                    try:
                        return int(float(row[n]))
                    except (TypeError, ValueError):
                        return None
            return None

        out[code] = Nutrition(
            kcal=pick("energy", "kcal", "calories"),
            protein_g=pick("protein", "proteinG"),
            fat_g=pick("fat", "fatG"),
            carb_g=pick("carbohydrate", "carb", "carbG"),
            sodium_mg=pick("sodium", "sodiumMg"),
            calcium_mg=pick("calcium", "calciumMg"),
        )
    return out


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
        """调用一个 MCP Tool。Token 失效时抛 TokenExpired，供上层降级。"""
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
        return data.get("result", {})


def load_fixture(name: str, fixture_dir: str) -> dict:
    """读取 fixtures 中录制的真实 MCP 响应。"""
    path = os.path.join(fixture_dir, f"{name}.json")
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)
