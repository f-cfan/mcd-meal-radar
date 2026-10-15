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

    @property
    def is_quoted(self) -> bool:
        return self.quoted_price is not None

    @property
    def name(self) -> str:
        return " + ".join(i.name for i in self.items)


@dataclass
class Decision:
    best: Candidate | None
    runners_up: list[Candidate]
    degraded_layers: list[str]
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# 层 1：偏好
# --------------------------------------------------------------------------

def apply_preference(items: list[MenuItem], profile: TasteProfile) -> list[Candidate]:
    """把单品展开成候选，并按偏好加权。

    这里只做**软排序**，不做硬过滤 —— 硬过滤留给营养层和预算层，
    否则「喜好但不健康」的情况会被过早杀掉。
    """
    cands: list[Candidate] = []
    top = set(profile.top_codes)

    for item in items:
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
    # 门店券优先；卡包券仅在无门店券时作为补充
    usable = list(store_coupons) or [
        c for c in my_coupons if c.expires_in_days is None or c.expires_in_days <= 7
    ]

    expanded: list[Candidate] = []
    for cand in candidates:
        if cand.rejected_by:
            expanded.append(cand)
            continue
        expanded.append(cand)

        for coupon in usable[:max_pairs]:
            clone = Candidate(
                items=list(cand.items), coupon=coupon,
                quoted_price=None, nutrition=cand.nutrition,
                score=cand.score,
                reasons=list(cand.reasons),
                warnings=list(cand.warnings),
            )
            src = "当前门店券" if coupon.coupon_id in store_ids else "卡包券"
            if coupon.value is not None:
                clone.score += Decimal(20)
                clone.reasons.append(f"叠加{src}「{coupon.title}」，面值 {coupon.value}")
            else:
                clone.warnings.append(f"{src}「{coupon.title}」面值无法从菜单推算，需实核")
            if coupon.expires_in_days is not None and coupon.expires_in_days <= 3:
                clone.score += Decimal(8)
                clone.reasons.append(f"该券 {coupon.expires_in_days} 天后过期")
            expanded.append(clone)

    return expanded


# --------------------------------------------------------------------------
# 枚举与计价分离
# --------------------------------------------------------------------------

def attach_quoted_prices(
    candidates: list[Candidate],
    price_table: dict[str, Money],
    coupon_price_table: dict[str, Money] | None = None,
) -> list[Candidate]:
    """用 calculate-price 的真实返回给候选定价 —— **枚举与计价严格分离**。

    官方 calculate-price 的入参不接 couponId，只接商品编码与数量；
    券的抵扣体现在返回的 discount 字段里。因此本函数用两个表：
      · price_table          「code:qty+…」 规范签名 → 无券实付
      · coupon_price_table   同一签名 + 券后的实付

    **绝不因为组合不在表里就估算价格** —— 查不到就留 None，
    邮件中显示「未核价」。这是本项目最重要的技术纪律。
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
    total = {k: 0 for k in ("kcal", "protein_g", "fat_g", "carb_g", "sodium_mg", "calcium_mg")}
    seen_any = False
    for item in items:
        n = table.get(item.code)
        if not n:
            continue
        seen_any = True
        for k in total:
            v = getattr(n, k)
            if v is not None:
                total[k] += v
    if not seen_any:
        return Nutrition()
    return Nutrition(**total)


def apply_nutrition(
    candidates: list[Candidate],
    nutrition_table: dict[str, Nutrition],
    policy: DietaryPolicy,
    relax_if_empty: bool = True,
) -> list[Candidate]:
    """营养硬上限过滤。

    若全部候选被营养约束杀掉，且 policy 允许，则放宽一档重试 ——
    空结果对用户毫无价值，给一个「超标但可选」的下限更实用。
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
            nutr = _nutr_sum(cand.items, nutrition_table)
            cand.nutrition = nutr

            if strict:
                if policy.max_kcal and nutr.kcal and nutr.kcal > policy.max_kcal:
                    cand.rejected_by = f"营养：热量 {nutr.kcal} kcal 超过上限 {policy.max_kcal}"
                    out.append(cand)
                    continue
                if policy.max_sodium_mg and nutr.sodium_mg and nutr.sodium_mg > policy.max_sodium_mg:
                    cand.rejected_by = f"营养：钠 {nutr.sodium_mg} mg 超过上限 {policy.max_sodium_mg}"
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


# --------------------------------------------------------------------------
# 预算过滤 + 排序 + 输出
# --------------------------------------------------------------------------

def apply_budget(candidates: list[Candidate], policy: DietaryPolicy) -> list[Candidate]:
    """预算过滤。**只对已核价的候选生效** —— 未核价不得因预算被淘汰，
    否则会把「其实买得起」的方案误杀。"""
    for cand in candidates:
        if cand.rejected_by or not cand.is_quoted or not policy.budget_yuan:
            continue
        if cand.quoted_price is not None and cand.quoted_price.yuan > policy.budget_yuan:
            cand.rejected_by = f"预算：实付 {cand.quoted_price} 超过 {policy.budget_yuan}"
    return candidates


def solve(snapshot: Snapshot, profile: TasteProfile, policy: DietaryPolicy,
          price_table: dict[str, Money] | None = None) -> Decision:
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

    cands = apply_preference(snapshot.menu, profile)

    if snapshot.my_coupons or snapshot.store_coupons:
        cands = enumerate_coupon_combinations(
            cands, snapshot.my_coupons, snapshot.store_coupons)
    else:
        degraded.append("优惠券")
        notes.append("未获取到优惠券，仅按原价建议")

    # 枚举完成后才定价 —— 顺序不可颠倒
    if price_table:
        cands = attach_quoted_prices(cands, price_table)
    else:
        degraded.append("计价")
        notes.append("未取得 calculate-price 报价，金额未核实")

    cands = apply_nutrition(cands, snapshot.nutrition, policy)
    cands = apply_campaign(cands, snapshot)
    cands = apply_budget(cands, policy)

    alive = [c for c in cands if c.rejected_by is None]
    # 已核价的候选优先展示 —— 给用户可执行的金额
    alive.sort(key=lambda c: (c.is_quoted, c.score), reverse=True)
    return Decision(
        best=alive[0] if alive else None,
        runners_up=alive[1:4],
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
