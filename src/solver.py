"""四层约束求解引擎。

四层依次求解，输出可解释的推荐 —— 每个结论都带「为什么」，不输出黑盒分数：

  偏好层 Preference  → 你吃你的（硬过滤 + 加权）
  钱包层 Wallet      → 你花你的（券组合枚举 + 真实核价）
  身体层 Body        → 你不能垮（热量 / 钠硬上限）
  活动层 Campaign    → 现在有什么（门店活动叠加提示）

设计原则：
  · **枚举与计价分离**：先枚举候选组合，再调 calculate-price 真实核价。
    绝不用模型估算价格。这是本项目最重要的技术纪律。
  · **可解释优先**：每个被淘汰的候选都保留淘汰原因，便于在邮件里说明。
  · **降级可用**：任一约束数据缺失时该层降级为「提示」，不阻塞其余层。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Iterable, Literal

from mcd_adapter import Coupon, MenuItem, Money, Nutrition, Snapshot

# 用户可配置：单餐营养上限（None 表示不约束）
@dataclass
class DietaryPolicy:
    """饮食约束。budget_yuan 允许传 str（用户手写配置的常见形式），
    __post_init__ 会统一归一化，避免比较时抛 TypeError。"""

    max_kcal: int | None = 900
    max_sodium_mg: int | None = 2000
    budget_yuan: Decimal | None = Decimal("45")
    excluded_keywords: tuple[str, ...] = ()
    keywords: dict[str, Decimal] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.budget_yuan is not None and not isinstance(self.budget_yuan, Decimal):
            self.budget_yuan = Decimal(str(self.budget_yuan))
        self.excluded_keywords = tuple(self.excluded_keywords or ())
        if self.max_kcal is not None:
            self.max_kcal = int(self.max_kcal)
        if self.max_sodium_mg is not None:
            self.max_sodium_mg = int(self.max_sodium_mg)


@dataclass
class TasteProfile:
    """偏好画像。数据来源 order-list 历史订单 + 用户显式声明。

    隐私约束：此结构仅存在于本地 data/ 目录（已被 .gitignore 排除），
    任何字段都不会发往第三方。
    """

    top_codes: tuple[str, ...] = ()
    liked_tags: dict[str, int] = field(default_factory=dict)
    disliked_keywords: tuple[str, ...] = ()
    favorite_store_code: str | None = None
    order_count: int = 0


@dataclass
class Candidate:
    """一个候选组合。"""

    items: list[MenuItem]
    coupon: Coupon | None
    quoted_price: Money | None  # calculate-price 真实返回；None = 未核价
    nutrition: Nutrition
    score: Decimal = Decimal(0)
    reasons: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    rejected_by: str | None = None
    # 实付费用构成（原始「分」）。外送场景与菜单标价不同，需向用户说明：
    # price 含配送费与打包费，只看 productPrice 会以为算错了。
    fee_breakdown: dict[str, int | None] = field(default_factory=dict)
    # 套餐子项名（来自官方 query-meal-detail 的 rounds + isDefault）。
    # ⚠️ 为什么必须单独记：套餐在菜单里是**整体一项**（"麦辣鸡腿汉堡三件套"），
    # 营养表里根本没有这个名字。要给出「换掉哪一样能省热量」的建议，
    # 就必须知道它由哪几样组成 —— 否则只能干巴巴地说「超过上限」。
    combo_parts: tuple[str, ...] = ()

    @property
    def is_quoted(self) -> bool:
        return self.quoted_price is not None

    @property
    def name(self) -> str:
        return " + ".join(i.name for i in self.items)

    def fee_note(self) -> str:
        """费用构成说明。实测外送场景 price = 商品 + 配送 + 打包。"""
        fb = self.fee_breakdown or {}
        delivery = fb.get("delivery")
        packing = fb.get("packing")
        product = fb.get("product")
        if not any((delivery, packing)):
            return ""
        bits = []
        if product is not None:
            bits.append(f"商品 ¥{Decimal(product) / 100:.2f}")
        if delivery:
            bits.append(f"配送 ¥{Decimal(delivery) / 100:.2f}")
        if packing:
            bits.append(f"打包 ¥{Decimal(packing) / 100:.2f}")
        if not bits:
            return ""
        return "实付构成：" + " + ".join(bits) + "（以官方页面为准）"

    def delivery_fee_ratio(self) -> Decimal | None:
        """配送+打包 占商品价的比例。数据不足时返回 None。

        这个比例是判断「该不该点外送」的关键：
        实测一份 ¥19.70 的单品外送，配送 ¥6.00 + 打包 ¥1.90，
        附加成本占商品价的 40% —— 比很多人以为的高得多。
        """
        fb = self.fee_breakdown or {}
        delivery = fb.get("delivery") or 0
        packing = fb.get("packing") or 0
        product = fb.get("product")
        if not product or product <= 0:
            return None
        return (Decimal(delivery + packing) / Decimal(product))

    def delivery_fee_warning(self, threshold: Decimal = Decimal("0.30")) -> str:
        """附加成本占比过高时提示到店自取 —— 纯算术，不是模型估算。

        ⚠️ 纪律：只在**官方 calculate-price 返回的费用构成**基础上算，
        绝不用菜单标价推算配送费（那是估算）。
        拿不到费用构成时返回空字符串，不猜。
        """
        ratio = self.delivery_fee_ratio()
        if ratio is None or ratio < threshold:
            return ""
        fb = self.fee_breakdown or {}
        extra = (Decimal((fb.get("delivery") or 0) + (fb.get("packing") or 0))
                 / Decimal(100))
        return (f"配送+打包 ¥{extra:.2f} 占商品价 {ratio:.0%} —— "
                f"到店自取可省这笔，值不值你自己定")


@dataclass
class Decision:
    best: Candidate | None
    runners_up: list[Candidate]
    degraded_layers: list[str]
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# 层 1：偏好
# --------------------------------------------------------------------------

# 非食品项关键词 —— 官方菜单里混着联名周边（实测有「麦当劳联名徽章」
# 「薯条脆卜卜毛绒周边」），它们被归在「人气热卖」里所以会被推荐出来，
# 但**晚饭推荐吃徽章显然是错的**。这类项直接硬过滤，不参与打分。
NON_FOOD_KEYWORDS: tuple[str, ...] = (
    "徽章", "毛绒", "周边", "玩具", "购物袋", "钥匙扣", "马克杯",
    "抱枕", "文具", "贴纸", "手机壳", "T恤", "积木", "扭蛋", "兑换",
)

# 早餐专供品关键词 —— 午晚餐时段推早餐品不合理。
# ⚠️ 刻意**不包含咖啡类**：实测「麦咖啡」是独立饮品分类，
# 全天都合适当饮品，不该被当成早餐品误杀。
BREAKFAST_ONLY_KEYWORDS: tuple[str, ...] = (
    "麦满分", "猪柳蛋", "培根蛋", "脆薯饼", "吉士蛋堡",
    "早安", "开工", "一夜", "营养早餐", "早餐桶",
)

# 哪些餐次不该出现早餐专供品
BREAKFAST_SLOTS: tuple[str, ...] = ("lunch", "tea", "dinner")


def apply_preference(items: list[MenuItem], profile: TasteProfile,
                     meal: str = "") -> list[Candidate]:
    """把单品展开成候选，并按偏好加权。

    这里只做**软排序**，硬过滤留给营养层和预算层（除两类例外，见下），
    否则「喜好但不健康」的情况会被过早杀掉。

    ⚠️ meal 参数用于时段错配过滤 —— 官方菜单里「人气热卖」混着早餐品，
    不加这道过滤会出现「晚饭推荐麦满分」。**不传 meal 则跳过滤**，
    保证离线 demo 与单测的行为与原版一致。
    """
    cands: list[Candidate] = []
    top = set(profile.top_codes)
    check_breakfast = meal in BREAKFAST_SLOTS

    for item in items:
        # 例外 1：非食品项硬过滤（推荐徽章/毛绒显然不对）
        if any(k in item.name for k in NON_FOOD_KEYWORDS):
            cands.append(Candidate(
                items=[item], coupon=None, quoted_price=None,
                nutrition=Nutrition(),
                rejected_by="偏好：非食品项（联名周边）",
            ))
            continue

        # 例外 2：午晚餐时段过滤早餐专供品
        if check_breakfast and any(k in item.name for k in BREAKFAST_ONLY_KEYWORDS):
            cands.append(Candidate(
                items=[item], coupon=None, quoted_price=None,
                nutrition=Nutrition(),
                rejected_by=f"偏好：早餐专供品，不适合{meal}时段",
            ))
            continue

        if any(bad in item.name for bad in profile.disliked_keywords):
            cands.append(Candidate(
                items=[item], coupon=None, quoted_price=None,
                nutrition=Nutrition(),
                rejected_by="偏好：命中忌口关键词",
            ))
            continue

        score = Decimal(0)
        reasons: list[str] = []
        if item.code in top:
            score += Decimal(50)
            reasons.append(f"命中你的常点「{item.name}」")
        for tag in item.tags:
            if tag in profile.liked_tags:
                score += Decimal(profile.liked_tags[tag])
                reasons.append(f"偏好标签「{tag}」×{profile.liked_tags[tag]}")
        if item.has_discount:
            score += Decimal(15)
            reasons.append(f"套餐结构自带优惠，标省 {item.discount}")

        cands.append(Candidate(
            items=[item], coupon=None, quoted_price=None,
            nutrition=Nutrition(), score=score, reasons=reasons,
        ))
    return cands


# --------------------------------------------------------------------------
# 层 2：钱包 —— 券组合枚举（核心）
# --------------------------------------------------------------------------

def enumerate_coupon_combinations(
    candidates: list[Candidate],
    my_coupons: list[Coupon],
    store_coupons: list[Coupon],
    max_pairs: int = 2,
) -> list[Candidate]:
    """枚举「单品 + 券」的候选组合。

    券来源两个，必须分开对待：
      · query-my-coupons  → 卡包全量，不校验门店
      · query-store-coupons → 当前门店可用
    **优先用门店券** —— 卡包券到店可能用不了，混用会产生无法核价的组合。

    券面价值（value）为 None 时不做金额预估，只标注「券面价值待核」。
    """
    store_ids = {c.coupon_id for c in store_coupons}

    # 候选券池：门店券 + 即将过期的卡包券。
    # ⚠️ 早期版本用 `list(store_coupons) or [...]` 短路 —— 有门店券就完全不看卡包券，
    # 且取 usable[:2] 是任意前两张，可能漏掉面值更大或更临期的券。
    # 现在合并两个来源并按「有效期紧迫度 → 面值」排序。
    card = [c for c in my_coupons
            if c.coupon_id not in store_ids
            and (c.expires_in_days is None or c.expires_in_days <= 7)]

    def urgency(c: Coupon) -> tuple:
        """排序键：快过期的优先；面值大的次之；面值未知的最后。"""
        days = c.expires_in_days if c.expires_in_days is not None else 9999
        value = c.value.yuan if c.value is not None else Decimal("-1")
        return (days, -value, c.coupon_id)

    usable = sorted(store_coupons + card, key=urgency)

    expanded: list[Candidate] = []
    for cand in candidates:
        if cand.rejected_by:
            expanded.append(cand)
            continue
        expanded.append(cand)

        cand_codes = {i.code for i in cand.items}
        # ⚠️ `max_pairs` 必须在**通过适用性校验之后**才计数。
        #
        # 真实 bug（2026-10-16 Gemini 复检发现）：早期写法是
        # `for coupon in usable[:max_pairs]` —— 先切片再校验适用性。
        # 若专属券排在第 3 位而 max_pairs=2，它会被切片直接丢掉，
        # 校验形同虚设。改为「校验通过 → paired += 1 → 攒够再 break」，
        # 这样 max_pairs 限制的是**真正配上的券数**，而不是被考察的券数。
        paired = 0
        for coupon in usable:
            # ⚠️ 券的**适用范围**校验（官方 products 字段）。
            #
            # 真实 bug（2026-10-16 用户复现「未核价」时顺带发现）：
            # 早期版本不校验适用范围，于是造出「无糖饮品立减券 + 麦辣鸡腿汉堡」
            # 这种官方根本不允许的组合。后果有两层：
            #   ① calculate-price 对该组合核不出价 → 显示「未核价」
            #   ② 更糟：它排在推荐首位，而用户下单时用不了这张券
            #
            # 实测当前门店 3 张券的适用商品（9900012387 / 9900016371 / 507411）
            # **都不在 123 项菜单里** —— 不校验就会持续推荐无法使用的券。
            #
            # 判定规则（保守）：
            #   · 官方给了 applies_to → 必须包含组合里**至少一个**商品，否则不生成
            #   · 官方没给 applies_to → 不做推测，沿用旧行为（宁可漏也不错配）
            if coupon.applies_to and not (cand_codes & set(coupon.applies_to)):
                continue

            clone = Candidate(
                items=list(cand.items), coupon=coupon,
                quoted_price=None, nutrition=cand.nutrition,
                score=cand.score,
                reasons=list(cand.reasons),
                warnings=list(cand.warnings),
            )
            src = "当前门店券" if coupon.coupon_id in store_ids else "卡包券"
            # ⚠️ 判「有面值」必须看**大于 0**，不能只看 `is not None`。
            # `_first_not_none` 修好 0 值之后，`value=0` 会变成 `Money(0)`，
            # 若仍按「非 None 即有面值」处理，就会给一张 0 元券加分并显示
            # 「面值 ¥0.00」—— 用户会以为这是笔真优惠。
            if coupon.value is not None and coupon.value.yuan > 0:
                clone.score += Decimal(20)
                clone.reasons.append(f"叠加{src}「{coupon.title}」，面值 {coupon.value}")
            elif coupon.value is not None:
                # 官方给了 0 —— 如实呈现，不当成优惠
                clone.warnings.append(f"{src}「{coupon.title}」官方面值为 0，不是真优惠")
            else:
                clone.warnings.append(f"{src}「{coupon.title}」面值无法从菜单推算，需实核")
            if coupon.expires_in_days is not None and coupon.expires_in_days <= 3:
                clone.score += Decimal(8)
                # ⚠️ 「今天到期」不能写成「0 天后过期」—— 后者读起来像 bug。
                exp_str = ("今天到期" if coupon.expires_in_days == 0
                           else f"{coupon.expires_in_days} 天后过期")
                clone.reasons.append(f"该券{exp_str}")
            expanded.append(clone)
            paired += 1
            if paired >= max_pairs:
                break

    return expanded


# --------------------------------------------------------------------------
# 枚举与计价分离
# --------------------------------------------------------------------------

def attach_quoted_prices(
    candidates: list[Candidate],
    price_table: dict[str, Money],
    coupon_price_table: dict[str, Money] | None = None,
    breakdowns: dict[str, dict] | None = None,
) -> list[Candidate]:
    """用 calculate-price 的真实返回给候选定价 —— **枚举与计价严格分离**。

    官方 calculate-price 的入参不接 couponId，只接商品编码与数量；
    券的抵扣体现在返回的 discount 字段里。因此本函数用两个表：
      · price_table          「code:qty+…」 规范签名 → 无券实付
      · coupon_price_table   同一签名 + 券后的实付

    **绝不因为组合不在表里就估算价格** —— 查不到就留 None，
    邮件中显示「未核价」。这是本项目最重要的技术纪律。

    breakdowns 是费用构成（原始「分」值），用于说明外送实付里
    商品 / 配送 / 打包各占多少。它是**显式参数**而非模块级全局：
    早期版本让 solver 反向 import collector 的可变全局，职责边界被打破，
    且 demo/离线场景下常常取不到。改为显式传参后依赖方向是单向的
    （collector → solver），且离线可用。
    """
    table = dict(price_table)
    if coupon_price_table:
        table.update(coupon_price_table)

    for cand in candidates:
        if cand.rejected_by or not cand.items:
            continue
        key = combo_signature([(i.code, 1) for i in cand.items])
        quoted = table.get(key)
        if quoted is not None:
            cand.quoted_price = quoted
            # 实测外送场景 price 含配送与打包费，需向用户说明构成
            fb = (breakdowns or {}).get(key)
            if fb:
                cand.fee_breakdown = fb
                note = cand.fee_note()
                if note:
                    cand.reasons.append(note)
                # 附加成本占比过高时给出到店自取提示（纯算术）
                warn = cand.delivery_fee_warning()
                if warn:
                    cand.warnings.append(warn)
            if cand.coupon is not None:
                cand.reasons.append("已通过 calculate-price 真实核价")
        else:
            # 未录入计价数据的组合不猜价，只如实说明
            cand.quoted_price = None
            cand.warnings.append("该组合未取得 calculate-price 报价，金额未核实")
    return candidates


def combo_signature(items: list[tuple[str, int]]) -> str:
    """商品组合的规范签名：按编码升序，形如 code:qty+code:qty。

    与官方 calculate-price 录制的 key 格式一致（见 fixtures/prices.json）。
    """
    return "+".join(f"{c}:{q}" for c, q in sorted(items, key=lambda x: x[0]))


# --------------------------------------------------------------------------
# 层 3：身体 —— 营养硬上限
# --------------------------------------------------------------------------

def _nutr_sum(items: Iterable[MenuItem], table: dict[str, Nutrition]) -> Nutrition:
    """按菜单编码累加营养。

    ⚠️ 必须**透传来源与完整度标记**（source / partial / missing）。
    早期版本只累加数值，把 query-meal-detail 拆解出来的
    「由官方子项相加、但有子项未覆盖」这个关键信息丢了 ——
    于是邮件把 649 kcal（部分和）当成全量展示，
    而用户会以为这就是这套餐的全部热量。这正是本项目最不能犯的错。
    """
    fields = ("kcal", "protein_g", "fat_g", "carb_g", "sodium_mg", "calcium_mg")
    total: dict = {k: 0 for k in fields}
    seen_any = False
    partial = False
    missing: list[str] = []
    sources: set[str] = set()

    for item in items:
        n = table.get(item.code)
        if not n:
            partial = True
            missing.append(item.name)
            continue
        seen_any = True
        sources.add(getattr(n, "source", "direct"))
        if getattr(n, "partial", False):
            partial = True
            missing.extend(getattr(n, "missing", ()) or ())
        for k in fields:
            v = getattr(n, k, None)
            if v is not None:
                total[k] += v

    if not seen_any:
        return Nutrition(partial=partial, missing=tuple(missing))

    return Nutrition(
        **total,
        # 来源只要有一个是拆解来的，整体就标 combo_sum
        source="combo_sum" if "combo_sum" in sources else "direct",
        partial=partial,
        missing=tuple(dict.fromkeys(missing)),   # 去重保序
    )


def _swap_hint_for_kcal(cand: "Candidate",
                        nutrition_table: dict[str, Nutrition],
                        limit: int,
                        by_name_table: dict[str, Nutrition] | None = None
                        ) -> str:
    """热量超标时，给出**具体可执行的替换建议**（数值全部来自官方营养表）。

    ⚠️ 为什么需要它（2026-10-16 Gemini 复检时暴露）：
    主力套餐「麦辣鸡腿汉堡三件套」真实营养 921 kcal
    （485 汉堡 + 289 中薯条 + 147 可乐中杯，全部官方数据），
    默认上限 900 kcal 下它被直接淘汰，邮件里只写「超过上限」——
    用户看到的是一句拒绝，拿不到任何可操作信息。

    **正确解法不是抬高阈值**：阈值来自用户健康需求，
    为了让某个商品入选去改它，等于让约束去服务于结论。
    应该告诉用户「换成什么能进上限」——
    无糖可口可乐中杯官方值 0 kcal / 35 mg 钠，正好省下 147 kcal。

    ⚠️ 关键：`cand.items` 里的套餐是**整体一项**（名字「麦辣鸡腿汉堡三件套」），
    在营养表里根本不存在。必须用 `combo_parts`（官方 query-meal-detail 拆出的子项）
    才能定位到「该换哪一样」。

    找不到可行替换时返回空串 —— 不编造建议。
    """
    total = _nutr_sum(cand.items, nutrition_table)
    if not total.kcal:
        return ""
    over = total.kcal - limit          # 需要至少省下的量
    if over <= 0:
        return ""

    # 优先在套餐子项上找替换 —— 套餐在菜单里是整体一项，
    # 只有知道它由哪几样组成，才谈得上「换掉哪一样」。
    parts = list(getattr(cand, "combo_parts", ()) or ())
    if parts:
        # ⚠️ 子项要按「改动优先级」遍历（饮品 → 小食 → 主食），
        # 不能按官方 rounds 的原始顺序 ——
        # 官方把主菜放第一个，于是第一个找到的可行替身总是"换主菜"，
        # 而用户宁愿换饮料。实测三件套就是这个坑。
        for part_name in sorted(parts, key=_category_rank):
            cur = _nutrition_by_name(part_name, by_name_table or nutrition_table)
            if cur is None or cur.kcal is None:
                continue
            hint = _find_swap(part_name, cur, over,
                              by_name_table or nutrition_table, limit)
            if hint:
                return hint
        return ""

    # 没有子项信息（单品组合）时，退回按菜单项本身找
    for item in cand.items:
        cur = _nutrition_of(item, nutrition_table, by_name_table)
        if cur is None or cur.kcal is None:
            continue
        hint = _find_swap(item.name, cur, over,
                          by_name_table or nutrition_table, limit)
        if hint:
            return hint
    return ""


def _find_swap(item_name: str, cur: Nutrition, over: int,
               table: dict[str, Nutrition], limit: int) -> str:
    """在同品类里找一个能省下 `over` kcal 的替身。找不到返回空串。

    ⚠️ 三条约束缺一不可，否则建议就是噪音：
      1. **同品类**（`_same_category`）—— 可乐不能换成红茶，口味不同不是等价替换
      2. **同规格** —— 中杯不能换成大杯，份量变了就不是「换一样」
      3. **刚好够用的最小改动** —— 省太多等于让用户牺牲本可以保留的那部分

    反例（实测踩过两次）：三件套 921 kcal 超 900，
      · 不做同品类判定 → 推「汉堡 → 猪柳麦满分」（换的是主菜，意图冲突）
      · 品类判得太松 → 推「可乐中杯 → 阳光柠檬红茶中杯」（147→117，只省 30）
      · 不排最小改动 → 推省得最多的那个，用户其实只需少 21 kcal
    正确答案是「可乐中杯 → 无糖可口可乐中杯」（147 → 0 kcal，同品类同规格）。
    """
    want_size = _cup_size(item_name)
    cands: list[tuple[int, int, str, Nutrition]] = []
    for name, nutr in table.items():
        if nutr.kcal is None or nutr.kcal >= cur.kcal:
            continue
        if not _same_category(item_name, name):
            continue
        if want_size and _cup_size(name) and _cup_size(name) != want_size:
            continue
        saved = cur.kcal - nutr.kcal
        if saved >= over:
            cands.append((_category_rank(item_name), saved - over, name, nutr))

    if not cands:
        return ""
    cands.sort(key=lambda t: (t[0], t[1], t[2]))
    _rank, _slack, name, nutr = cands[0]
    saved = cur.kcal - nutr.kcal
    return (f"把「{item_name}」换成「{name}」，"
            f"少 {saved} kcal（{cur.kcal} → {nutr.kcal}），"
            f"就能控制在 {limit} kcal 内")


def _cup_size(name: str) -> str:
    """从商品名提取杯/份规格：小 / 中 / 大。提取不到返回空串。"""
    for size in ("小", "中", "大"):
        if f"{size}杯" in name or f"{size}薯条" in name or f"({size})" in name:
            return size
    return ""


def _nutrition_by_name(name: str, table: dict[str, Nutrition]):
    """按名称（含规格归一）直接取官方营养，缺失返回 None。"""
    from mcd_adapter import normalize_name
    want = normalize_name(name)
    for n, nutr in table.items():
        if normalize_name(n) == want:
            return nutr
    return None


def _nutrition_of(item, nutrition_table: dict[str, Nutrition],
                  by_name_table: dict[str, Nutrition] | None = None):
    """按名称（含规格归一）取一个菜单商品的官方营养，缺失返回 None。

    先查按名称索引的表（标准单品表），再查按编码索引的表（菜单表）。
    """
    if by_name_table:
        hit = _nutrition_by_name(item.name, by_name_table)
        if hit is not None:
            return hit
    return _nutrition_by_name(item.name, nutrition_table)


# 同品类判定：用于「换一样」建议的**严格**分类。
#
# ⚠️ 刻意比「都是饮料」更严：可乐换成红茶虽然都是饮品，但口味完全不同，
# 不是等价替换。实测三件套场景下，宽松判定会推荐「可乐中杯 → 阳光柠檬红茶中杯」
# （147 → 117 kcal，只省 30），而真正该推荐的是同规格的
# 「无糖可口可乐中杯」（147 → 0 kcal，直接达标）。
# 建议的可信度取决于它是不是**真的等价** —— 宁可少给，不给错。
_CATEGORY_WORDS = (
    ("无糖可口可乐", "可乐"), ("可口可乐", "可乐"), ("可乐", "可乐"),
    ("红茶", "红茶"), ("绿茶", "绿茶"), ("奶茶", "奶茶"),
    ("咖啡", "咖啡"),
    ("薯条", "薯条"), ("麦乐鸡", "麦乐鸡"), ("派", "派"), ("玉米杯", "玉米杯"),
    ("汉堡", "汉堡"), ("麦满分", "麦满分"),
)
# 换东西时的优先级：饮料 > 小食 > 主食
_CATEGORY_RANK = {
    "可乐": 0, "红茶": 1, "绿茶": 1, "奶茶": 1, "咖啡": 1,
    "薯条": 2, "麦乐鸡": 2, "派": 2, "玉米杯": 2,
    "汉堡": 3, "麦满分": 3,
}


def _same_category(a: str, b: str) -> bool:
    """两个商品名是否属于同一品类（严格：口味也须一致）。

    两边都没命中品类词时返回 False —— 不给建议总比给错建议好。
    """
    def cat(name: str) -> str:
        for word, tag in _CATEGORY_WORDS:
            if word in name:
                return tag
        return ""
    ca, cb = cat(a), cat(b)
    return bool(ca) and ca == cb


def _category_rank(name: str) -> int:
    """品类改动优先级，数字越小越优先换。"""
    for word, tag in _CATEGORY_WORDS:
        if word in name:
            return _CATEGORY_RANK.get(tag, 9)
    return 9


def apply_nutrition(
    candidates: list[Candidate],
    nutrition_table: dict[str, Nutrition],
    policy: DietaryPolicy,
    relax_if_empty: bool = True,
    by_name_table: dict[str, Nutrition] | None = None,
    combo_parts_map: dict[str, tuple[str, ...]] | None = None,
) -> list[Candidate]:
    """营养硬上限过滤。

    若全部候选被营养约束杀掉，且 policy 允许，则放宽一档重试 ——
    空结果对用户毫无价值，给一个「超标但可选」的下限更实用。

    `by_name_table` 是**按名称索引的标准单品营养表**（官方 list-nutrition-foods），
    用于在套餐子项里找替换方案 —— 菜单编码索引的表里没有「中薯条」这类子项名。
    `combo_parts_map` 是 商品编码 → 套餐子项名（官方 query-meal-detail 拆解结果）。
    """
    def pass_once(strict: bool) -> list[Candidate]:
        out: list[Candidate] = []
        for cand in candidates:
            if cand.rejected_by:
                out.append(cand)
                continue
            if any(bad in cand.name for bad in policy.excluded_keywords):
                cand.rejected_by = f"偏好：命中排除关键词"
                out.append(cand)
                continue
            # 记录套餐子项，供「换哪一样能省热量」的建议使用
            if combo_parts_map:
                for _item in cand.items:
                    parts = combo_parts_map.get(_item.code)
                    if parts:
                        cand.combo_parts = tuple(parts)
                        break

            nutr = _nutr_sum(cand.items, nutrition_table)
            cand.nutrition = nutr

            if strict:
                if policy.max_kcal and nutr.kcal and nutr.kcal > policy.max_kcal:
                    cand.rejected_by = (
                        f"营养：热量 {nutr.kcal} kcal 超过上限 {policy.max_kcal}")
                    hint = _swap_hint_for_kcal(cand, nutrition_table,
                                              policy.max_kcal, by_name_table)
                    if hint:
                        cand.warnings.append(hint)
                    out.append(cand)
                    continue
                if policy.max_sodium_mg and nutr.sodium_mg and nutr.sodium_mg > policy.max_sodium_mg:
                    cand.rejected_by = (
                        f"营养：钠 {nutr.sodium_mg} mg 超过上限 {policy.max_sodium_mg}")
                    out.append(cand)
                    continue
            else:
                if policy.max_kcal and nutr.kcal and nutr.kcal > policy.max_kcal:
                    cand.warnings.append(f"热量 {nutr.kcal} kcal 超出你设定的 {policy.max_kcal}")
                if policy.max_sodium_mg and nutr.sodium_mg and nutr.sodium_mg > policy.max_sodium_mg:
                    cand.warnings.append(f"钠 {nutr.sodium_mg} mg 超出你设定的 {policy.max_sodium_mg}")

            if nutr.kcal is None:
                cand.warnings.append("该组合营养数据未核实")
            out.append(cand)
        return out

    result = pass_once(strict=True)
    if not any(c.rejected_by is None for c in result) and relax_if_empty:
        result = pass_once(strict=False)
        for c in result:
            if c.rejected_by and c.rejected_by.startswith("营养"):
                c.rejected_by = None
    return result


# --------------------------------------------------------------------------
# 层 4：活动 —— 门店活动叠加
# --------------------------------------------------------------------------

def apply_campaign(candidates: list[Candidate], snapshot: Snapshot) -> list[Candidate]:
    """活动层只做提示，不改分数 —— 活动信息时效性强，不该主导排序。"""
    if not snapshot.campaign_names:
        return candidates
    live = "、".join(snapshot.campaign_names[:3])
    for cand in candidates:
        if cand.rejected_by is None:
            cand.warnings.append(f"当前门店活动：{live}（结账以官方页面为准）")
    return candidates


def apply_budget(candidates: list[Candidate], policy: DietaryPolicy) -> list[Candidate]:
    """预算过滤。**只对已核价的候选生效** —— 未核价不得因预算被淘汰，
    否则会把「其实买得起」的方案误杀。"""
    for cand in candidates:
        if cand.rejected_by or not cand.is_quoted or not policy.budget_yuan:
            continue
        if cand.quoted_price is not None and cand.quoted_price.yuan > policy.budget_yuan:
            cand.rejected_by = f"预算：实付 {cand.quoted_price} 超过 {policy.budget_yuan}"
    return candidates


def apply_festival_boost(
    candidates: list[Candidate],
    festival: object | None = None,
) -> list[Candidate]:
    """中国国家法定节假日应景加分（温和软加权，不突破营养与预算硬上限）。

    当命中法定节假日时，对契合节日聚餐、出行或时令解腻场景的标签或关键词
    加一个温和分值（+8~10 分），并在 reasons 中写入可解释说明。
    """
    if festival is None:
        return candidates
    boost_tags = set(getattr(festival, "boost_tags", ()) or ())
    boost_keywords = tuple(getattr(festival, "boost_keywords", ()) or ())
    boost_score = Decimal(int(getattr(festival, "boost_score", 8) or 8))
    boost_reason = str(getattr(festival, "boost_reason", "") or "")
    if not (boost_tags or boost_keywords):
        return candidates

    for cand in candidates:
        if cand.rejected_by:
            continue
        hit_tag = any(
            tag in boost_tags
            for item in cand.items
            for tag in item.tags
        )
        hit_kw = any(kw in cand.name for kw in boost_keywords)
        if hit_tag or hit_kw:
            cand.score += boost_score
            if boost_reason and boost_reason not in cand.reasons:
                cand.reasons.append(boost_reason)
    return candidates


# --------------------------------------------------------------------------
# 零成本预筛（决定「值得花钱核价的是哪几个」）
# --------------------------------------------------------------------------

def prefilter_for_pricing(
    candidates: list[Candidate],
    top_k: int = 8,
) -> list[Candidate]:
    """在调 calculate-price 之前，先用**零成本**的约束筛出值得核价的候选。

    ⚠️ 为什么必须先筛再核价
    `calculate-price` 是逐次调用的。菜单动辄上百项，若对每个候选都核价：
      · API 调用量爆炸（限流 600 次/分，且完全没必要）
      · 绝大多数候选会被后续的预算/营养约束杀掉，核了也白核
      · 邮件里出现大量「未核价」占位 —— 那不是诚实，是没做完

    早期版本就是「先核价再筛选」，实测 77 个候选里 59 个显示未核价。
    现在改为：
      偏好层（本地打分）→ 预算预判（用菜单标价，只作**筛除**不作展示）
      → 取前 K 个 → 只对这 K 个调 calculate-price

    关键区别：菜单标价在这里只用于**淘汰明显超预算的**，
    绝不用于展示给用户 —— 展示的金额仍然只来自 calculate-price 真实返回。
    这条界线不能模糊：一旦用标价报价，就回到了「估算」的老路。
    """
    alive = [c for c in candidates if c.rejected_by is None]
    if not alive:
        return []

    # 预算预筛：用菜单标价做**粗筛**。单商品标价 > 预算上限的必不可能入选。
    # 注意：组合的总价在核价前算不准（套餐组合、配送打包费未知），
    # 因此这里只做单品级的粗筛，宁可放过也不误杀。
    rough = []
    for c in alive:
        single_sum = sum(i.price.yuan for i in c.items)
        if single_sum > Decimal("200"):   # 宽松阈值，只滤明显离谱的
            continue
        rough.append(c)

    if not rough:
        rough = alive

    rough.sort(key=lambda c: c.score, reverse=True)

    # ⚠️ **去重必须发生在截断之前** —— 这是个被外部评审抓到的真 bug。
    #
    # enumerate_coupon_combinations 会把同一商品拆成
    # 「无券版 + 券1 版 + 券2 版」三个候选，它们的 combo 完全相同
    # （都是 [(code, 1)]），但常点商品底分高（+50），于是这几个克隆体
    # 会挤在一起占满 top_k 的坑位。
    #
    # 如果「先截断再去重」（早期版本），2 张券时结果是这样的：
    #   候选池里前 3 名的 9 个克隆体塞满 top_k=8 → 去重后只剩 3 款餐品
    # 正确做法是边去重边攒，攒够 top_k 个**不同商品**：
    #   实测 2 张券时能覆盖 7~8 款不同餐品
    #
    # 去重只影响「送去核价的请求」，不影响候选本身 ——
    # 券版与非券版共用同一个 calculate-price 结果，符合官方语义
    # （calculate-price 入参不接 couponId，券的抵扣体现在返回的 discount 里）。
    picked: list[Candidate] = []
    seen: set[str] = set()
    for c in rough:
        sig = combo_signature([(i.code, 1) for i in c.items])
        if sig in seen:
            continue
        seen.add(sig)
        picked.append(c)
        if len(picked) >= top_k:
            break

    return picked


def solve(snapshot: Snapshot, profile: TasteProfile, policy: DietaryPolicy,
          price_table: dict[str, Money] | None = None,
          breakdowns: dict[str, dict] | None = None,
          meal: str = "",
          festival: object | None = None) -> Decision:
    """四层依次求解，返回带解释的决策。

    price_table 是 calculate-price 的真实返回（签名 → Money）。
    线上由 collector 现场调用 calculate-price 生成；demo 从 fixtures 读。
    查不到价的组合**不估算**，标为「未核价」。
    """
    degraded: list[str] = []
    notes: list[str] = []

    if snapshot.degraded:
        degraded.append("实时数据")
        notes.append(f"实时数据不可用（{snapshot.degrade_reason}），以下基于缓存快照")

    if not snapshot.menu:
        return Decision(None, [], ["菜单"], notes=["未获取到菜单数据，无法生成建议"])

    cands = apply_preference(snapshot.menu, profile, meal)

    if snapshot.my_coupons or snapshot.store_coupons:
        cands = enumerate_coupon_combinations(
            cands, snapshot.my_coupons, snapshot.store_coupons)
    else:
        degraded.append("优惠券")
        notes.append("未获取到优惠券，仅按原价建议")

    # 枚举完成后才定价 —— 顺序不可颠倒
    if price_table:
        cands = attach_quoted_prices(cands, price_table, breakdowns=breakdowns)
    else:
        degraded.append("计价")
        notes.append("未取得 calculate-price 报价，金额未核实")

    # 把「按名称索引的标准单品表」与「套餐子项表」一起传下去 ——
    # 只有这两张表能让营养层给出「换掉套餐里哪一样能省热量」的具体建议。
    cands = apply_nutrition(cands, snapshot.nutrition, policy,
                            by_name_table=snapshot.nutrition_by_name or None,
                            combo_parts_map=snapshot.combo_parts or None)
    cands = apply_campaign(cands, snapshot)
    cands = apply_budget(cands, policy)
    cands = apply_festival_boost(cands, festival)
    # 口味轮换放在最后：前几层筛完后，只对真正有机会当选的候选降权
    cands = apply_rotation(cands)

    alive = [c for c in cands if c.rejected_by is None]

    # ⚠️ 展示层最后一道纪律：**未核价候选不占推荐位**。
    #
    # 「不估算价格」有两层含义，第二层常被忽略：
    #   · 不编造价格        —— 已实现（quoted_price 为 None 就是没有）
    #   · 不用无法执行的价格占位 —— 本段实现
    #
    # 只做「已核价优先」的排序是不够的：当已核价候选不足 3 个时，
    # 未核价候选仍会被填进 runners_up，邮件里就出现
    # 「麦辣鸡腿汉堡 · 未核价」这种用户无法执行的条目。
    # 用户看到没有金额的推荐，价值为零 —— 那不是诚实，是没做完。
    #
    # 因此：只要存在已核价候选，就只从已核价候选里选。
    # 全部未核价时才回退展示，并在 notes 里说明金额未核实。
    quoted = [c for c in alive if c.is_quoted]
    unquoted = [c for c in alive if not c.is_quoted]

    if quoted:
        shown = quoted
        if unquoted:
            notes.append(
                f"另有 {len(unquoted)} 个候选未取得官方报价，未列入推荐"
                f"（宁可少推，也不给无法执行的价格）")
    else:
        shown = unquoted
        if unquoted:
            degraded.append("计价")
            notes.append(
                f"{len(unquoted)} 个候选均未取得 calculate-price 报价，"
                f"金额未核实，建议以官方小程序为准")

    # 已核价候选优先展示 —— 给用户可执行的金额
    shown.sort(key=lambda c: (c.is_quoted, c.score), reverse=True)
    return Decision(
        best=shown[0] if shown else None,
        runners_up=shown[1:4],
        degraded_layers=degraded,
        notes=notes,
    )


# --------------------------------------------------------------------------
# 本地偏好画像读写（敏感信息，仅本地）
# --------------------------------------------------------------------------

def load_profile(path: str) -> TasteProfile:
    """读取本地偏好画像。文件缺失时返回空画像 —— 这是正常的首次运行状态。"""
    if not os.path.exists(path):
        return TasteProfile()
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    return TasteProfile(
        top_codes=tuple(raw.get("top_codes", [])),
        liked_tags={k: int(v) for k, v in (raw.get("liked_tags") or {}).items()},
        disliked_keywords=tuple(raw.get("disliked_keywords", [])),
        favorite_store_code=raw.get("favorite_store_code"),
        order_count=int(raw.get("order_count", 0)),
    )


# --------------------------------------------------------------------------
# 口味轮换：避免「复读机效应」
# --------------------------------------------------------------------------

# 冷却衰减分。24 小时内推过的餐品降权，让位给次优选项。
#
# ⚠️ 取值说明：常点商品底分是 +50，若惩罚太小（如 -25），
# 轮换形同虚设 —— 实测降权后它仍稳居第一，等于没轮换。
# 这里取 -55，略高于常点加分，使「今天推过了」优先于「你爱吃」，
# 但**不排除它**（若无其他候选或它明显更优，仍会回到第一）。
COOLDOWN_PENALTY = Decimal("55")
# 记录最近多少次推送
ROTATION_MEMORY = 3
# 冷却有效期（小时）
COOLDOWN_HOURS = 24


def load_rotation(path: str = "data/rotation.json") -> list[dict]:
    """读取最近推送记录。每条形如 {"code": "...", "at": "2026-10-16T11:00:00"}。"""
    import json
    import os
    if not os.path.exists(path):
        return []
    try:
        with open(path, encoding="utf-8") as fh:
            data = json.load(fh)
        return data if isinstance(data, list) else []
    except (ValueError, OSError):
        return []


def save_rotation(entries: list[dict],
                  path: str = "data/rotation.json") -> None:
    """追加一条推送记录，只保留最近 ROTATION_MEMORY 条。"""
    import json
    import os
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(entries[-ROTATION_MEMORY:], fh,
                      ensure_ascii=False, indent=2)
    except OSError:
        pass          # 轮换记录写不进去不该影响主流程


def apply_rotation(candidates: list[Candidate],
                   history: list[dict] | None = None,
                   now: datetime | None = None,
                   penalty: Decimal = COOLDOWN_PENALTY) -> list[Candidate]:
    """给 24 小时内推过的餐品降权，避免天天复读同一份。

    ⚠️ 动机：常点商品底分 +50，若无冷却，一天四档任务会
    周一中午、周一下午、周二中午… 永远推同一份。
    用户看三天就会把邮件拖进垃圾箱 —— 这是**体验层面的隐伤**。

    降权后若它仍排第一，说明确实最优，保留；
    只有当别的选项分数更高时才发生轮换。

    冷却窗口内的记录会被清理（超过 24 小时自动失效）。
    """
    history = history if history is not None else load_rotation()
    if not history:
        return candidates

    now = now or datetime.now()
    # 支持带时区与不带时区两种记录
    fresh: set[str] = set()
    for h in history:
        code = h.get("code")
        at = h.get("at")
        if not code or not at:
            continue
        try:
            t = datetime.fromisoformat(str(at))
        except ValueError:
            continue
        if t.tzinfo is not None:
            t = t.replace(tzinfo=None)
        delta = now.replace(tzinfo=None) - t
        if delta.total_seconds() <= COOLDOWN_HOURS * 3600:
            fresh.add(code)

    if not fresh:
        return candidates

    for c in candidates:
        if c.rejected_by:
            continue
        hit_items = [i for i in c.items if i.code in fresh]
        if not hit_items:
            continue
        c.score -= penalty
        names = "、".join(i.name for i in hit_items)
        c.warnings.append(
            f"已为你轮换口味 —— 常点的「{names}」24 小时内推过，本轮暂放备选")
    return candidates


def record_push(code: str, now: datetime | None = None,
                path: str = "data/rotation.json") -> None:
    """记录本轮推送的餐品编码。"""
    now = now or datetime.now()
    stamp = now.replace(microsecond=0).isoformat()
    entries = load_rotation(path)
    entries.append({"code": code, "at": stamp})
    save_rotation(entries, path)
