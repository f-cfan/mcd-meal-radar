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
    parse_order_list,
    parse_meal_detail,
    nutrition_from_combo,
    build_nutrition_alias_index,
    find_low_sodium_swap,
)

FIXTURE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")
CACHE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")

CST = timezone(timedelta(hours=8))


def _pick_address(client: McdMcpClient) -> str | None:
    """按 MCD_ADDRESS_PICK 从地址簿选一个地址。

    默认**不启用** —— 「发到哪个地址」是有后果的选择，
    不该静默取第一条。只有用户显式设了 MCD_ADDRESS_PICK 才会生效：

      MCD_ADDRESS_PICK=first     取地址簿第一项
      MCD_ADDRESS_PICK=1         取第 2 项（索引从 0 起，故 1 表示第二项）

    这与「绝不静默假设」是同一条纪律：能自动的自动化，不能猜的不猜。
    """
    pick = os.environ.get("MCD_ADDRESS_PICK", "").strip()
    if not pick:
        return None
    try:
        payload = client.call_tool("delivery-query-addresses", {})
    except MCPError:
        return None
    data = (payload.get("data") or {})
    addrs = data.get("addresses") or data.get("list") or []
    if not addrs:
        return None
    if pick.lower() == "first":
        return addrs[0].get("addressId")
    try:
        idx = int(pick)
    except ValueError:
        return None
    if 0 <= idx < len(addrs):
        return addrs[idx].get("addressId")
    return None


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


def enrich_combo_nutrition(client: McdMcpClient, snapshot: Snapshot,
                           store_code: str, combos: list,
                           be_code: str | None = None,
                           be_type: int = 2) -> tuple[dict, dict]:
    """对送核价的套餐调 query-meal-detail，补齐营养数据。

    ⚠️ 为什么需要这一步
    官方 `list-nutrition-foods` 只覆盖**标准单品**（实测 158 项），
    而菜单以**套餐**为主（实测 131 项里大量是「X四件套」）。
    直接按名称匹配，营养命中率只有约 22% ——
    也就是说「身体层约束」在真实推荐面前基本被架空。

    官方其实给了 `query-meal-detail` 能返回套餐的子项构成
    （含每个替换项与差价），把它和标准单品营养表联表相加，
    命中率能提升到 75% 以上，**且全部来自官方数据，不含任何估算**。

    只对**送去核价的套餐**调用（通常 8 个），不对全店 131 项调 ——
    API 调用量与限流（600次/分）都可控。

    返回 (补齐后的营养表, 降钠建议表)。
    """
    if not combos or not snapshot.menu:
        return {}, {}

    # ⚠️ 这里要的是「**标准单品**的营养表」，按名称索引。
    # snapshot.nutrition 是按菜单编码索引的，而菜单里 90% 是套餐 ——
    # 直接拿它联表会把「四件套」当成单品去匹配，命中极低。
    #
    # 正确做法：单独调 list-nutrition-foods 拿标准单品表（按名称），
    # 用它给套餐子项联表。实测这能把套餐命中率从 ~0 提到 75%。
    nutrition_by_name: dict = {}
    try:
        nutrition_by_name = parse_nutrition_list(
            client.call_tool("list-nutrition-foods", {}))
    except Exception as exc:
        print(f"  [营养表不可用] {type(exc).__name__}")
        return {}, {}
    if not nutrition_by_name:
        return {}, {}

    enriched: dict = {}
    swaps: dict = {}
    for combo in combos:
        if len(combo) != 1:
            # 目前只处理单品级套餐；组合的构成需递归，暂不支持
            continue
        code = combo[0][0]
        item = next((m for m in snapshot.menu if m.code == code), None)
        if not item:
            continue
        try:
            payload = client.call_tool("query-meal-detail", {
                "code": code,
                "storeCode": store_code,
                "beType": be_type,
                "orderType": 2 if be_type in (2, 5, 6) else 1,
                **({"beCode": be_code} if be_code else {}),
            })
        except Exception as exc:
            print(f"  [套餐详情跳过] {item.name} → {type(exc).__name__}")
            continue

        # ⚠️ 官方数据坑（实测 2026-10-16）：同一编码在 query-meals 与
        # query-meal-detail 里**名称可能对不上**（9900008752 菜单叫
        # 「无糖可乐(中)」，详情却叫「派」）。名称不一致时挂营养 = 张冠李戴，
        # 这里宁可跳过，只报未核实。
        detail_name = str(((payload or {}).get("data") or {}).get("name") or "")
        if detail_name and detail_name != item.name:
            print(f"  ⚠️ 套餐营养跳过 {item.name}（{code}）："
                  f"官方详情名「{detail_name}」与菜单名不一致，不做猜测")
            continue

        # ⚠️ 传 nutrition_by_name（按名称索引）而非 snapshot.nutrition（按编码索引）：
        # 套餐子项（如「中薯条」）在菜单里根本不存在，只有名称表里查得到。
        part_enriched, part_swaps = apply_combo_nutrition(
            payload, nutrition_by_name)
        if part_enriched:
            enriched.update(part_enriched)
        if part_swaps:
            swaps.update(part_swaps)
    return enriched, swaps


def apply_combo_nutrition(payload: dict, nutrition_by_name: dict) -> tuple[dict, dict]:
    """把一份 query-meal-detail 响应解成营养增量与低钠替换建议。

    ⚠️ 抽成独立函数的原因：`enrich_combo_nutrition`（线上真实调用）与
    `load_snapshot_from_fixtures`（离线 demo）都需要这一步。
    早期版本只有线上走这条路径，于是 fixture 生成的示例邮件里
    套餐营养全是「未核实」—— 预览与真实效果不一致。
    逻辑只有一份，两个入口共用，才不会出现这种漂移。
    """
    from mcd_adapter import (build_nutrition_alias_index, find_low_sodium_swap,
                             nutrition_from_combo, parse_meal_detail)

    detail = parse_meal_detail(payload)
    if not detail.get("groups"):
        return {}, {}

    alias_index = build_nutrition_alias_index(nutrition_by_name)
    total, matched, _unmatched = nutrition_from_combo(
        detail, nutrition_by_name, alias_index)
    if not matched:
        return {}, {}

    enriched = {}
    # 一个响应只对应一个商品；code 从响应里取，避免调用方再传一遍
    code = str(((payload or {}).get("data") or {}).get("code") or "")
    if not code:
        data = (payload or {}).get("data")
        if isinstance(data, str):
            try:
                code = str((json.loads(data).get("data") or {}).get("code") or "")
            except (ValueError, AttributeError):
                code = ""
    if code:
        enriched[code] = total

    swaps: dict = {}
    if total.sodium_mg:
        swap = find_low_sodium_swap(detail, nutrition_by_name,
                                    total.sodium_mg, alias_index)
        if swap:
            swaps[code] = swap
    return enriched, swaps


def plan_quotes(snapshot: Snapshot, profile, policy, top_k: int = 8,
                meal: str = ""):
    """决定「问哪些组合的价」—— 零成本预筛，不发任何请求。

    ⚠️ 这是「未核价」问题的根治点。
    早期版本直接对菜单前 N 项调 calculate-price，实测 77 个候选里
    59 个显示「未核价」—— 那不是诚实，是没做完。

    现在先跑一遍完整的本地求解（偏好 → 券枚举 → 预筛），
    只把**最可能被选中的 K 个**送去核价：
      · API 调用量从数十次降到个位数
      · 邮件里的「未核价」占位从 59 个降到 0~2 个

    返回 (待核价组合列表, 预筛后的候选)。
    """
    from solver import (apply_preference, combo_signature,
                       enumerate_coupon_combinations, prefilter_for_pricing)

    if not snapshot.menu:
        return [], []

    cands = apply_preference(snapshot.menu, profile, meal)
    if snapshot.my_coupons or snapshot.store_coupons:
        cands = enumerate_coupon_combinations(
            cands, snapshot.my_coupons, snapshot.store_coupons)

    picked = prefilter_for_pricing(cands, top_k=top_k)

    # 去重已在 prefilter_for_pricing **内部**完成（必须先于 top_k 截断，
    # 否则 2 张券时 top8 会被同一商品的克隆体占满，去重后只剩 3~4 款餐品）。
    # 这里只需把已去重的候选转成请求列表。
    combos = [[(i.code, 1) for i in c.items] for c in picked]
    return combos, picked


def quote_candidates(client: McdMcpClient, store_code: str, scene: str,
                     combos: list[list[tuple[str, int]]],
                     address_id: str | None = None,
                     be_code: str | None = None,
                     take_way_code: str | None = None
                     ) -> tuple[dict[str, object], dict[str, dict]]:
    """线上核价：对每个候选组合调用 calculate-price 取真实金额。

    **这是本项目唯一的价格来源。** 没有 MCP 返回就没有金额，
    绝不调用任何模型估算或用标签价格推算。

    ⚠️ 实测踩坑（两个，都会伪装成"商品售罄"）：
      ① 入参必须按场景矩阵齐全：`storeCode / beType / orderType`，
         外送再加 `beCode + addressId`，到店再加 `takeWayCode`。
         漏传 `beType` 或 `takeWayCode` 会返回「响应无 price 字段」——
         看起来像售罄，实则是参数缺失。本函数已统一走
         `mcd_adapter.build_price_args`，四个场景共用同一份逻辑。
      ② 每次调用都要校验 `data.price` 非空才写入计价表。
         空响应说明这次调用没拿到价格，绝不能当成"价格是 0"。

    另外注意：官方 calculate-price 的入参不接 couponId，券的抵扣体现在返回的
    discount 字段里。因此同一个商品组合在有无券时可能对应不同签名，
    这里按传入的组合签名原样记录返回值，不做任何推测。

    返回 (计价表, 费用构成表) —— 两者一起传给 solver，
    避免用模块级可变全局传递状态（早期版本用 LAST_BREAKDOWNS，
    导致 solver 反向依赖 collector，职责边界被打破）。
    """
    from mcd_adapter import build_price_args, parse_price, parse_price_breakdown
    from solver import combo_signature

    table: dict[str, object] = {}
    breakdowns: dict[str, dict] = {}
    for combo in combos:
        key = combo_signature(combo)
        try:
            args = build_price_args(scene, store_code, combo,
                                    be_code=be_code,
                                    address_id=address_id,
                                    take_way_code=take_way_code)
        except MCPError as exc:
            # 参数矩阵不满足时**不静默跳过**：整批核价都不可信
            print(f"  [计价中止] {key} → {exc}")
            print("  → 场景必填参数缺失，放弃本轮核价（宁可标未核价，也不猜价）")
            return table, breakdowns

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
    return table, breakdowns


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

    nutrition_by_name = parse_nutrition_list(load_fixture("nutrition", FIXTURE_DIR))
    # 先按名称把标准单品营养映射到菜单编码 —— 与线上 build_nutrition_by_code 同一路径。
    # ⚠️ 漏掉这步的症状很隐蔽：营养表是按**名称**索引的，而求解器按**编码**查，
    # 于是「麦辣鸡腿汉堡」明明在表里（485 kcal/1208 mg），查出来却是 None。
    nutrition = build_nutrition_by_code(menu, nutrition_by_name)

    # 套餐营养补齐 —— 与线上 enrich_combo_nutrition 走同一个 apply_combo_nutrition。
    # ⚠️ 早期版本只有线上补套餐营养，fixture 路径没补，
    # 于是 demo / 预览邮件里套餐营养全是「未核实」，预览与真实效果不一致。
    # 官方 list-nutrition-foods 只覆盖标准单品（实测 158 项），
    # 套餐必须靠 query-meal-detail 的 rounds + isDefault 拆解后联表相加。
    # ⚠️ 联表用的是**按名称索引的原始表**（nutrition_by_name），不是按编码索引的
    # nutrition —— 套餐子项（如「中薯条」）在菜单里根本不存在，只有名称表里有。
    combo_path = os.path.join(FIXTURE_DIR, "real_combo_details.json")
    if os.path.exists(combo_path):
        with open(combo_path, encoding="utf-8") as fh:
            details = (json.load(fh) or {}).get("details") or {}
        for _code, _payload in details.items():
            # ⚠️ 官方数据坑（实测 2026-10-16）：同一个商品编码在
            # `query-meals` 与 `query-meal-detail` 里**名称对不上**。
            # 例：9900008752 在菜单里叫「无糖可乐(中)」，
            # 但 query-meal-detail 返回的 name 是「派」，默认项是「菠萝派」。
            # 若不校验，会把菠萝派的营养算到无糖可乐头上（221 kcal/147 mg钠），
            # 而真实无糖可乐约 2 kcal —— 差 100 倍，直接误导用户控钠控热量。
            #
            # 修法：**只接受 detail.name 与菜单名一致的组合营养**，
            # 不一致就跳过并告警。宁可显示「未核实」，也不能挂上别人的营养。
            _detail_name = str(((_payload or {}).get("data") or {}).get("name") or "")
            _menu_item = next((m for m in menu if m.code == _code), None)
            if _menu_item and _detail_name and _detail_name != _menu_item.name:
                print(f"  ⚠️ 套餐营养跳过 {_code}：菜单名「{_menu_item.name}」"
                      f"≠ 官方详情名「{_detail_name}」（官方编码复用，不做猜测）")
                continue
            _enriched, _ = apply_combo_nutrition(_payload, nutrition_by_name)
            nutrition.update(_enriched)

    # 券 —— 优先用真实录制（含官方 products 适用范围），缺失才回落到文档样本。
    # ⚠️ 为什么要优先真实券：官方 `query-store-coupons` 每张券都带
    # `products: [{productCode, productName}]` 适用范围。文档样本里没有这个字段，
    # 于是 solver 无从校验，会造出「无糖饮品券 + 汉堡」这种用不了的组合。
    real_coupons_path = os.path.join(FIXTURE_DIR, "real_store_coupons.json")
    if os.path.exists(real_coupons_path):
        with open(real_coupons_path, encoding="utf-8") as fh:
            store_coupons = parse_coupons(json.load(fh), store_scoped=True)
    else:
        store_coupons = parse_coupons(
            load_fixture("store_coupons", FIXTURE_DIR), store_scoped=True)

    return Snapshot(
        fetched_at=menu_payload.get("_captured_at", _now_cst()),
        scene=scene,
        source="fixture",
        store=primary_store,
        menu=menu,
        nutrition=nutrition,
        my_coupons=parse_coupons(load_fixture("my_coupons", FIXTURE_DIR)),
        store_coupons=store_coupons,
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
    snap = Snapshot(fetched_at=_now_cst(), scene=scene, store=None, source="live")
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

    # 历史订单 —— 用于推导偏好画像。
    # 实测：新账号返回 data:{}（空对象），parse_order_list 会优雅降级为 []
    snap.orders = parse_order_list(client.call_tool("order-list", {}))

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
        source="cache",
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

    ⚠️ 外送场景必须有 addressId —— 官方 delivery-query-stores 必填该参数，
    缺了会返回「缺少参数」。本函数按以下顺序解析：
      1. 显式传入的 address_id
      2. 环境变量 MCD_ADDRESS_ID
      3. 调 delivery-query-addresses 取地址簿第一项（需指定 MCD_ADDRESS_PICK）
    第 3 步是刻意设计的：让用户零配置也能跑通外送链路，
    但**发到哪个地址**必须用户明确，不能静默猜 —— 因此需要设
    MCD_ADDRESS_PICK=first（或索引序号）才会启用。
    """
    if mode == "fixture":
        return load_snapshot_from_fixtures(scene)

    client = McdMcpClient(token=token)
    try:
        if auto_claim:
            # 09:00 任务：自动领取所有当前可用券
            # 这是唯一会改变账户状态的调用，故与所有只读调用物理隔离
            client.call_tool("auto-bind-coupons", {})

        # 外送场景解析地址：显式 → 环境变量 → 用户显式授权的「取第一项」
        if scene in ("delivery", "group") and not address_id:
            address_id = os.environ.get("MCD_ADDRESS_ID") or _pick_address(client)
        snap = fetch_live_snapshot(client, scene, address_id)
        save_cache(snap)
        return snap
    except TokenExpired as exc:
        cached = load_cached_snapshot()
        if cached:
            return cached
        return Snapshot(fetched_at=_now_cst(), scene=scene, store=None,
                        source="live", degraded=True,
                        degrade_reason=f"MCP Token 失效：{exc}")
    except MCPError as exc:
        cached = load_cached_snapshot()
        if cached:
            return cached
        return Snapshot(fetched_at=_now_cst(), scene=scene, store=None,
                        source="live", degraded=True,
                        degrade_reason=f"MCP 调用失败：{exc}")
