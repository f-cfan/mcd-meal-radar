"""定时任务调度器。

四档任务，全部可配置：

  09:00  morning  早间总览 + 自动领券（避开饭点高峰）
  11:00  lunch    午餐建议
  16:30  dinner   晚餐建议
  20:00  expiry   过期券/积分提醒

用法：
  python scheduler.py --fixture          # 离线跑全部四档（无需 Token/SMTP）
  python scheduler.py --live --now lunch  # 真实运行指定档位
  python scheduler.py --install-cron      # 打印 crontab 配置建议
  python scheduler.py --send fixture      # 渲染并实际发送邮件

**部署形态说明**：本脚本不自带常驻进程。推荐两种方式：
  1. 系统 cron（Linux/macOS）
  2. WorkBuddy 自动化任务（推荐，见 docs/SCHEDULE.md）
"""

from __future__ import annotations

import argparse
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timezone, timedelta
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from collector import get_snapshot, plan_quotes, quote_candidates  # noqa: E402
from mcd_adapter import (  # noqa: E402
    McdMcpClient,
    build_taste_profile_from_orders,
    profile_summary as build_profile_summary,
)
from mail import describe_channels, push  # noqa: E402
from mailer import (  # noqa: E402
    MEAL_WINDOWS, CareContext, current_meal_window,
    render_digest, render_plain, render_token_expired,
)
from mcd_adapter import MCPError  # noqa: E402
from solver import DietaryPolicy, Decision, TasteProfile, load_profile, solve  # noqa: E402

CST = timezone(timedelta(hours=8))

PROFILE_PATH = "data/profile.json"

# 送核价的候选数量上限。
# 8 个足够填满「主推 1 + 备选 3」，且把 API 调用控制在个位数。
QUOTE_TOP_K = 8


@dataclass
class Slot:
    key: str
    label: str
    hour: int
    minute: int
    auto_claim: bool = False


SLOTS = [
    Slot("morning", "早间总览", 9, 0, auto_claim=True),
    Slot("lunch", "午间", 11, 0),
    Slot("dinner", "晚间", 16, 30),
    Slot("expiry", "过期提醒", 20, 0),
]


def load_policy() -> DietaryPolicy:
    """读取用户饮食约束。缺省值是宽松的，不阻塞首次运行。"""
    import json
    import os
    path = os.path.join("data", "policy.json")
    if not os.path.exists(path):
        return DietaryPolicy()
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    return DietaryPolicy(
        max_kcal=raw.get("max_kcal"),
        max_sodium_mg=raw.get("max_sodium_mg"),
        budget_yuan=Decimal(str(raw["budget_yuan"])) if raw.get("budget_yuan") else None,
        excluded_keywords=tuple(raw.get("excluded_keywords") or []),
    )


def resolve_profile(snapshot, manual: TasteProfile | None = None) -> TasteProfile:
    """画像来源优先级：**自动推导 > 本地缓存 > 手工配置**。

    为什么自动推导优先 —— 个性化的价值全在「基于你自己的历史」，
    手工配置只是冷启动兜底。

    手工配置的忌口词始终保留（它是用户主动声明的，推导不应该覆盖），
    也不覆盖自动推导出的常点商品。
    """
    derived = build_taste_profile_from_orders(snapshot.orders, snapshot.menu)
    cached = load_profile(PROFILE_PATH)

    if derived.order_count:
        # 有历史订单 → 自动推导为主，忌口词用「推导 ∪ 手工」
        merged_kw = tuple(sorted(set(derived.disliked_keywords)
                                 | set(cached.disliked_keywords)
                                 | set(manual.disliked_keywords if manual else ())))
        derived.disliked_keywords = merged_kw
        # 手工若声明了标签权重，与推导结果合并（手工优先级更高）
        merged_tags = dict(derived.liked_tags)
        merged_tags.update(cached.liked_tags)
        if manual:
            merged_tags.update(manual.liked_tags)
        derived.liked_tags = merged_tags
        return derived

    # 无历史订单 → 回落到缓存，再回落到手工配置
    return cached if cached.order_count or cached.top_codes else (manual or TasteProfile())


def _window_passed(window: str, minutes: int) -> bool:
    """判断某个餐次时段是否已经结束（当前时刻已过其窗口末尾）。

    用于「今天错过了几个餐次」的判定 —— 只有**已经过去**的餐次才算漏，
    正在进行的和还没到的都不算。
    """
    start, end = MEAL_WINDOWS[window]
    return minutes > end


def build_care_context(snapshot, slot, now=None) -> CareContext:
    """从真实数据构造关怀上下文。

    **只传有依据的信号** —— 例如「漏餐」必须在历史订单里真的缺失该餐次，
    不能凭空说「你该吃饭了」。这是「不编造」纪律在文案层的延伸：
    没有数据支撑的关怀就是空话，读者能感觉到。
    """
    now = now or datetime.now(CST)
    meal = current_meal_window(now.hour)

    coupons = snapshot.store_coupons or snapshot.my_coupons
    # 分两档统计：今天就到期（更紧迫）与 3 天内到期。
    # ⚠️ expiresInDays == 0 按官方语义就是「今天到期」，不能和「还有 1 天」混为一谈 ——
    # 否则会出现「说今天过期、实际还有 1 天」这种不准确的提醒。
    expiring_today = [c for c in coupons if c.expires_in_days == 0]
    expiring_soon = [c for c in coupons
                     if c.expires_in_days is not None and 0 < c.expires_in_days <= 3]

    # 漏餐判定：仅当有历史订单时才算，否则视为「未知」而非「漏了」
    #
    # ⚠️ 早期版本这里是 `skipped = 0 if ordered_today else 1`，
    # 而 mailer 的触发条件是 `skipped_meals >= 2` —— 于是这个值最大只能是 1，
    # 导致 README 主打的那句「今天还没顾上吃饭」在真实运行中**永远不会被触发**。
    #
    # 现在改为**统计当天已经错过的餐次数量**，语义也和字段名一致：
    #   · 用真实历史订单判定当天有没有下单（无历史订单 → 0，不猜）
    #   · 已过时段数 - 已下单次数 = 错过的餐次
    # 这样「晚餐档发现一天没吃」会得到 skipped=3（早/午/晚都过了），
    # 自然满足 >=2 的触发条件。
    skipped = 0
    if snapshot.orders:
        # ⚠️ 早期版本用 `orderId.endswith("20261016")` 判「今天是否下单」，
        # 但真实订单号（ORD-20261016-123 / ORD-20261016123 / 带前缀哈希）
        # **都不以日期结尾**，这个条件恒为假 —— 于是「今天已下单」永远识别不到，
        # 漏餐判定在有订单的情况下也会误报。
        # 改为「日期出现在 ID 中」或「createdAt 以今天开头」，两种格式都覆盖。
        stamp = now.strftime("%Y%m%d")
        dash = now.strftime("%Y-%m-%d")
        ordered_today = [
            o for o in snapshot.orders
            if stamp in str(o.get("orderId", "") or "")
            or stamp in str(o.get("orderNo", "") or "")
            or str(o.get("createdAt", "") or "").startswith(dash)
            or str(o.get("orderTime", "") or "").startswith(dash)
        ]
        if not ordered_today:
            # 已过去的餐次时段数（早餐/午餐/下午茶/晚餐/夜市，凌晨不计入）
            passed_windows = sum(
                1 for name in ("breakfast", "lunch", "tea", "dinner")
                if _window_passed(name, now.hour * 60 + now.minute)
            )
            skipped = max(0, passed_windows)

    return CareContext(
        meal=meal,
        hour=now.hour,
        expiring_coupon_count=len(expiring_today) + len(expiring_soon),
        expiring_today=len(expiring_today),
        campaign_count=len(snapshot.campaign_names),
        skipped_meals=skipped,
        is_weekend=now.weekday() >= 5,
    )


def profile_summary(profile: TasteProfile, menu=None) -> str:
    """画像摘要。显示在邮件底部 —— 让用户知道建议基于什么。

    用 mcd_adapter.profile_summary 以复用「人话化」逻辑。
    """
    return build_profile_summary(profile, menu)


def run_slot(
    slot: Slot,
    mode: str = "fixture",
    do_send: bool = False,
    outdir: str = "out",
) -> dict:
    """执行一个档位任务。返回执行摘要。

    **核心纪律**：无论何种模式，本函数都不调用任何下单类工具。
    推荐归推荐，下单权始终在人。
    """
    import os
    os.makedirs(outdir, exist_ok=True)

    # 1) 抓数据（外送场景的 address_id 由 collector 按 MCD_ADDRESS_ID /
    #    MCD_ADDRESS_PICK 解析，见 _pick_address 的注释）
    snap = get_snapshot(mode=mode, scene="delivery", auto_claim=slot.auto_claim)

    token_dead = mode == "live" and snap.degraded and "Token" in (snap.degrade_reason or "")

    # 2) 求解
    if token_dead:
        decision: Decision = Decision(None, [], ["实时数据"], ["Token 失效"])
    else:
        # 画像只解析一次，既用于求解也用于渲染摘要（早期版本重复调用两次）
        profile = resolve_profile(snap)
        policy = load_policy()

        # ① 零成本预筛：先决定「问哪些组合的价」，不发请求。
        #    早期版本是「先核价再筛选」，导致邮件里 59/77 显示未核价 ——
        #    那是没做完，不是诚实。
        # 当前餐次 —— 用于过滤「晚餐推荐麦满分」这类时段错配
        meal_now = current_meal_window(datetime.now(CST).hour)

        price_table, breakdowns = {}, {}
        if mode == "live" and snap.menu and not snap.degraded:
            combos, _picked = plan_quotes(snap, profile, policy,
                                          top_k=QUOTE_TOP_K, meal=meal_now)
            if combos and snap.store:
                price_table, breakdowns = quote_candidates(
                    McdMcpClient(), snap.store.store_code, "delivery", combos,
                    address_id=os.environ.get("MCD_ADDRESS_ID") or None,
                    be_code=snap.store.be_code,
                )

        # ② 求解（纯本地计算，不发请求）
        decision = solve(snap, profile, policy, price_table, breakdowns, meal=meal_now)
        profile_text = profile_summary(profile, snap.menu)

    # 3) 渲染
    if token_dead:
        subject = "【麦麦饭点雷达】需要重新授权"
        html = render_token_expired("", snap.degrade_reason or "Token 失效")
        text = "MCP Token 已失效，请到 open.mcd.cn/mcp 重新获取并更新 MCD_MCP_TOKEN。"
    else:
        subject = f"【麦麦饭点雷达】{slot.label}建议 · {datetime.now(CST):%m-%d}"
        care = build_care_context(snap, slot)
        html = render_digest(decision, snap, profile_text, slot.label, care=care)
        text = render_plain(decision, snap, slot.label, care=care)

    # 4) 落盘（便于离线预览与调试）
    safe = slot.key
    html_path = os.path.join(outdir, f"{safe}.html")
    with open(html_path, "w", encoding="utf-8") as fh:
        fh.write(html)
    with open(os.path.join(outdir, f"{safe}.txt"), "w", encoding="utf-8") as fh:
        fh.write(text)

    result = {
        "slot": slot.key, "label": slot.label, "mode": mode,
        "degraded": snap.degraded, "degrade_reason": snap.degrade_reason,
        "html_path": html_path, "text_path": os.path.join(outdir, f"{safe}.txt"),
        "menu_items": len(snap.menu), "has_decision": decision.best is not None,
        "sent": False, "send_note": None, "channel": "none",
    }

    # 5) 推送（Agent Mail 优先，失败自动回落 SMTP）
    if do_send:
        res = push("", subject, html, text)
        result["sent"], result["send_note"] = res.ok, res.note
        result["channel"] = res.channel

    return result


def current_slot(now: datetime | None = None) -> Slot | None:
    """判断当前时间该跑哪一档。落在两档之间时不重复跑。"""
    now = now or datetime.now(CST)
    minutes = now.hour * 60 + now.minute
    for slot in SLOTS:
        target = slot.hour * 60 + slot.minute
        if 0 <= minutes - target < 30:  # 到点后 30 分钟窗口内有效
            return slot
    return None


CRONTAB_SAMPLE = """# 麦麦饭点雷达 · crontab 示例（北京时间）
# 注意：crontab 用系统时区，若服务器不在 UTC+8 请自行换算

0  9  * * *  cd /path/to/mcd-meal-radar && python scheduler.py --live --now morning
0  11 * * *  cd /path/to/mcd-meal-radar && python scheduler.py --live --now lunch
30 16  * * *  cd /path/to/mcd-meal-radar && python scheduler.py --live --now dinner
0  20  * * *  cd /path/to/mcd-meal-radar && python scheduler.py --live --now expiry

# 手动发一封（调试用）
# python scheduler.py --fixture --send"""


def main() -> int:
    ap = argparse.ArgumentParser(description="麦麦饭点雷达调度器")
    ap.add_argument("--live", action="store_true", help="走真实 MCP（需 Token）")
    ap.add_argument("--fixture", action="store_true",
                    help="走本地 fixtures（默认行为，显式写出更清晰）")
    ap.add_argument("--now", choices=[s.key for s in SLOTS], help="指定档位")
    ap.add_argument("--all", action="store_true", help="离线跑全部四档")
    ap.add_argument("--send", action="store_true", help="实际发送邮件（默认仅落盘）")
    ap.add_argument("--install-cron", action="store_true", help="打印 crontab 示例")
    args = ap.parse_args()

    if args.install_cron:
        print(CRONTAB_SAMPLE)
        return 0

    mode = "live" if args.live else "fixture"

    targets: list[Slot] = []
    if args.all or (not args.now):
        targets = SLOTS if args.all else (
            [s for s in [current_slot()] if s] or SLOTS)
    else:
        targets = [s for s in SLOTS if s.key == args.now]

    print(f"模式：{mode}（{'需 MCD_MCP_TOKEN' if mode == 'live' else '离线 fixtures，无需 Token'}）")
    print(f"推送：{describe_channels()}")
    print("-" * 62)

    failures = 0
    for slot in targets:
        try:
            res = run_slot(slot, mode=mode, do_send=args.send)
        except MCPError as exc:
            print(f"[{slot.label}] MCP 异常：{exc}")
            failures += 1
            continue

        flag = "（降级）" if res["degraded"] else ""

        # 菜单为 0 是「静默失败」的高发点，必须显式诊断
        if res["menu_items"] == 0:
            hint = ""
            if mode == "live":
                hint = ("  ← 外送场景需配送地址：请设置 MCD_ADDRESS_ID，"
                        "或设 MCD_ADDRESS_PICK=first 让程序从地址簿取第一项")
            print(f"[{slot.label}] ⚠️ 菜单 0 项 —— 未取到门店菜单{hint}")
            if res["degraded"]:
                print(f"    降级原因：{res['degrade_reason']}")
            continue
        sent = ""
        if args.send:
            if res["sent"]:
                sent = f" · 已通过 {res['channel']} 发送"
            else:
                sent = f" · 未发送：{res['send_note']}"
        print(f"[{slot.label}]{flag} 菜单 {res['menu_items']} 项 · "
              f"建议{'已生成' if res['has_decision'] else '未生成'} → {res['html_path']}{sent}")
        if res["degrade_reason"]:
            print(f"    降级原因：{res['degrade_reason']}")

    print("-" * 62)
    if failures:
        print(f"完成，但有 {failures} 个档位异常。")
    else:
        print("全部档位完成。用浏览器打开 out/*.html 可预览邮件效果。")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
