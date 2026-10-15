"""抓取编排层 —— 决定「数据从哪来」。

两个数据源，同一套出口（Snapshot）：

  live    走真实 MCP，需要 Token。用于定时任务实际运行。
  fixture 走本地录制，用于 demo、单测与 Token 失效时的降级快照。

**关键设计**：`load_snapshot` 是唯一入口，业务层不关心数据来自哪。
这样 demo 与线上跑的是同一套求解器，避免「演示能跑、线上不跑」。
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone, timedelta
from decimal import Decimal

from mcd_adapter import (
    McdMcpClient,
    MCPError,
    TokenExpired,
    Snapshot,
    Store,
    load_fixture,
    parse_coupons,
    parse_meals,
    parse_price,
    parse_nutrition_list,
    build_nutrition_by_code,
    parse_points,
    parse_stores,
    parse_campaign_titles,
)

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

CST = timezone(timedelta(hours=8))

# 最近一次 quote_candidates 的费用构成，供 solver 填充候选。
# 用模块级容器而非返回值扩展，是为了让既有 quote_candidates 签名不变、
# 不破坏 demo 与单测的调用方式。
LAST_BREAKDOWNS: dict[str, dict] = {}


def _now_cst() -> str:
    return datetime.now(CST).strftime("%Y-%m-%d %H:%M:%S")


# --------------------------------------------------------------------------
# fixtures → Snapshot
# --------------------------------------------------------------------------

def load_price_table_from_fixtures() -> dict[str, object]:
    """从 fixtures 读取 calculate-price 录制表：组合签名 → Money。

    这是「枚举与计价分离」的离线部分。线上则由 quote_candidates 现场调 calculate-price。
    """
    from mcd_adapter import Money, parse_price

    path = os.path.join(FIXTURE_DIR, "prices.json")
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    return {
        sig: parse_price(payload)
        for sig, payload in (raw.get("prices") or {}).items()
    }


def quote_candidates(client: McdMcpClient, store_code: str, scene: str,
                     combos: list[list[tuple[str, int]]],
                     address_id: str | None = None,
                     be_code: str | None = None) -> dict[str, object]:
    """线上核价：对每个候选组合调用 calculate-price 取真实金额。

    **这是本项目唯一的价格来源。** 没有 MCP 返回就没有金额，
    绝不调用任何模型估算或用标签价格推算。

    ⚠️ 实测踩坑：入参字段名必须是 storeCode / beCode / orderType / beType，
    少了 beCode 会返回 success 但 data 为空（表现为价格 0）。
    故每次调用都校验响应，非空才写入计价表。

    另外注意：官方 calculate-price 的入参不接 couponId，券的抵扣体现在返回的
    discount 字段里。因此同一个商品组合在有无券时可能对应不同签名，
    这里按传入的组合签名原样记录返回值，不做任何推测。
    """
    from mcd_adapter import parse_price, parse_price_breakdown
    from solver import combo_signature

    is_delivery = scene in ("delivery", "group")

    table: dict[str, object] = {}
    breakdowns: dict[str, dict] = {}
    for combo in combos:
        key = combo_signature(combo)
        args: dict = {
            "storeCode": store_code,
            "orderType": 2 if is_delivery else 1,
            "items": [{"productCode": c, "quantity": q} for c, q in combo],
        }
        if is_delivery:
            args["beType"] = 2 if scene == "delivery" else 6
            if be_code:
                args["beCode"] = be_code
            if address_id:
                args["addressId"] = address_id

        try:
            payload = client.call_tool("calculate-price", args)
        except MCPError as exc:
            print(f"  [计价跳过] {key} → {exc}")
            continue

        data = payload.get("data") or {}
        if not data or data.get("price") is None:
            # 计价失败不阻塞整体：该组合留空，邮件显示「未核价」
            print(f"  [计价跳过] {key} → 响应无 price 字段"
                  f"（{(payload or {}).get('message', '未知原因')}）")
            continue
        table[key] = parse_price(payload)
        breakdowns[key] = parse_price_breakdown(payload)
    LAST_BREAKDOWNS.clear()
    LAST_BREAKDOWNS.update(breakdowns)
    return table


def load_snapshot_from_fixtures(scene: str = "delivery") -> Snapshot:
    """从本地录制数据构造 Snapshot。demo 与单测的入口。"""
    menu_payload = load_fixture("menu", FIXTURE_DIR)
    status = json.load(open(os.path.join(FIXTURE_DIR, "store_menu_status.json"), encoding="utf-8"))
    stores_payload = json.load(open(os.path.join(FIXTURE_DIR, "stores.json"), encoding="utf-8"))
    prices = json.load(open(os.path.join(FIXTURE_DIR, "prices.json"), encoding="utf-8"))["prices"]

    menu = parse_meals(menu_payload, scene)

    # store_menu_status.json 的结构是 { data: { storeCode, unavailableCodes, soldOutNote } }
    status_data = status.get("data") or status
    primary_code = status_data.get("storeCode")

    # 剔除本店不可售商品 —— 官方不同门店菜单不同
    unavailable = set(status_data.get("unavailableCodes") or [])
    menu = [m for m in menu if m.code not in unavailable]

    primary_store = None
    for raw in stores_payload.get("data", {}).get("stores", []):
        if raw["storeCode"] == primary_code:
            primary_store = Store(
                store_code=raw["storeCode"], name=raw["name"],
                distance_km=Decimal(str(raw.get("distance", 0))) if raw.get("distance") else None,
                address=raw.get("address"), has_party=bool(raw.get("hasParty")),
            )
            break

    return Snapshot(
        fetched_at=menu_payload.get("_captured_at", _now_cst()),
        scene=scene,
        store=primary_store,
        menu=menu,
        nutrition=parse_nutrition_list(load_fixture("nutrition", FIXTURE_DIR)),
        my_coupons=parse_coupons(load_fixture("my_coupons", FIXTURE_DIR)),
        store_coupons=parse_coupons(load_fixture("store_coupons", FIXTURE_DIR), store_scoped=True),
        points=(load_fixture("points", FIXTURE_DIR).get("data") or {}).get("available"),
        campaign_names=[c["title"] for c in
                        (load_fixture("campaign_calendar", FIXTURE_DIR).get("data") or {})
                        .get("campaigns", []) if c.get("status") in ("ongoing", "upcoming")],
    )


# --------------------------------------------------------------------------
# live → Snapshot
# --------------------------------------------------------------------------

def fetch_live_snapshot(client: McdMcpClient, scene: str = "delivery",
                        address_id: str | None = None) -> Snapshot:
    """走真实 MCP 抓取。

    只调用**只读**工具。整个项目从不调用 create-order / draw-lottery /
    mall-create-order / party-order-create —— 推荐归推荐，下单权在人。

    Token 失效时抛 TokenExpired，由调用方降级。
    """
    snap = Snapshot(fetched_at=_now_cst(), scene=scene, store=None)
    be_type = 2 if scene in ("delivery", "group") else None  # 实测：2=麦乐送，6=团餐

    # 门店 —— 实测：beType 必填；data 是数组
    stores_args: dict = {"beType": be_type}
    if address_id:
        stores_args["addressId"] = address_id
    stores = parse_stores(client.call_tool("delivery-query-stores", stores_args))
    if stores:
        snap.store = stores[0]

    # 菜单 —— 实测：外送场景 storeCode + beCode + orderType + beType 四者齐全才通过
    if snap.store:
        menu_args: dict = {
            "storeCode": snap.store.store_code,
            "orderType": 2 if scene in ("delivery", "group") else 1,
        }
        if be_type:
            menu_args["beType"] = be_type
            if snap.store.be_code:
                menu_args["beCode"] = snap.store.be_code
        snap.menu = parse_meals(client.call_tool("query-meals", menu_args), scene)

    # 营养 —— 实测返回 DSL 字符串且表头无编码，需按名称联表
    nutr_by_name = parse_nutrition_list(client.call_tool("list-nutrition-foods", {}))
    snap.nutrition = build_nutrition_by_code(snap.menu, nutr_by_name)

    # 券：两个来源分开抓，语义不同不可混用
    snap.my_coupons = parse_coupons(client.call_tool("query-my-coupons", {}))
    if snap.store:
        snap.store_coupons = parse_coupons(
            client.call_tool("query-store-coupons", {"storeCode": snap.store.store_code}),
            store_scoped=True)

    # 积分与活动 —— 实测字段名 availablePoint；活动返回人读文本
    snap.points = parse_points(client.call_tool("query-my-account", {}))
    snap.campaign_names = parse_campaign_titles(client.call_tool("campaign-calendar", {}))

    return snap


# --------------------------------------------------------------------------
# 缓存：Token 失效时的降级数据源
# --------------------------------------------------------------------------

def save_cache(snapshot: Snapshot) -> None:
    """把成功抓取的快照落盘，供 Token 失效时兜底。"""
    os.makedirs(CACHE_DIR, exist_ok=True)
    payload = {
        "cached_at": snapshot.fetched_at,
        "scene": snapshot.scene,
        "store": {"store_code": snapshot.store.store_code, "name": snapshot.store.name,
                  "be_code": snapshot.store.be_code} if snapshot.store else None,
        "menu": [{"code": m.code, "name": m.name, "category": m.category,
                  "tags": list(m.tags), "price": str(m.price.yuan),
                  "original_price": str(m.original_price.yuan)} for m in snapshot.menu],
        "coupons": [{"coupon_id": c.coupon_id, "title": c.title,
                     "value": str(c.value.yuan) if c.value else None,
                     "expires_in_days": c.expires_in_days}
                    for c in (snapshot.store_coupons or snapshot.my_coupons)],
        "points": snapshot.points,
        "campaign_names": snapshot.campaign_names,
    }
    path = os.path.join(CACHE_DIR, "last_snapshot.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)


def load_cached_snapshot() -> Snapshot | None:
    """读取缓存快照。没有缓存时返回 None —— 调用方需处理首次运行的空态。"""
    path = os.path.join(CACHE_DIR, "last_snapshot.json")
    if not os.path.exists(path):
        return None
    from mcd_adapter import Coupon, MenuItem, Money

    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)

    menu = [MenuItem(code=m["code"], name=m["name"], category=m["category"],
                     tags=tuple(m.get("tags") or []),
                     price=Money(Decimal(m["price"])),
                     original_price=Money(Decimal(m["original_price"])))
            for m in raw.get("menu", [])]
    coupons = [Coupon(coupon_id=c["coupon_id"], title=c["title"],
                      value=Money(Decimal(c["value"])) if c.get("value") else None,
                      threshold=None, expires_in_days=c.get("expires_in_days"))
               for c in raw.get("coupons", [])]
    store = raw.get("store")
    return Snapshot(
        fetched_at=raw.get("cached_at", "未知"),
        scene=raw.get("scene", "delivery"),
        store=Store(store_code=store["store_code"], name=store["name"],
                    be_code=store.get("be_code")) if store else None,
        menu=menu, my_coupons=coupons,
        points=raw.get("points"), campaign_names=raw.get("campaign_names", []),
        degraded=True,
        degrade_reason=f"缓存快照（抓取于 {raw.get('cached_at', '未知')}）",
    )


# --------------------------------------------------------------------------
# 统一入口
# --------------------------------------------------------------------------

def get_snapshot(mode: str = "fixture", scene: str = "delivery",
                 token: str | None = None, address_id: str | None = None,
                 auto_claim: bool = False) -> Snapshot:
    """**唯一数据入口**。

    mode="fixture" → 离线，供 demo / 测试
    mode="live"    → 真实 MCP，供定时任务

    live 模式下 Token 失效会自动降级为缓存快照（degraded=True），
    调用方可据此在邮件中说明「本次为缓存数据」。
    """
    if mode == "fixture":
        return load_snapshot_from_fixtures(scene)

    client = McdMcpClient(token=token)
    try:
        if auto_claim:
            # 09:00 任务：自动领取所有当前可用券
            # 这是唯一会改变账户状态的调用，故与所有只读调用物理隔离
            client.call_tool("auto-bind-coupons", {})
        snap = fetch_live_snapshot(client, scene, address_id)
        save_cache(snap)
        return snap
    except TokenExpired as exc:
        cached = load_cached_snapshot()
        if cached:
            return cached
        return Snapshot(fetched_at=_now_cst(), scene=scene, store=None,
                        degraded=True, degrade_reason=f"MCP Token 失效：{exc}")
    except MCPError as exc:
        cached = load_cached_snapshot()
        if cached:
            return cached
        return Snapshot(fetched_at=_now_cst(), scene=scene, store=None,
                        degraded=True, degrade_reason=f"MCP 调用失败：{exc}")
