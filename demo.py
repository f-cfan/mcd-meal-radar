#!/usr/bin/env python3
"""麦麦饭点雷达 · 30 秒演示。

零第三方依赖 · 不需要 MCP Token · 不联网。

    python demo.py

演示四条完整链路，全部基于 fixtures/ 中录制的真实 MCP 响应：
  链路 A  午间推荐     → 四层约束求解，输出可解释建议
  链路 B  Token 失效   → 优雅降级，发送续授权提醒（这是本项目独有）
  链路 C  券过期提醒   → 制造紧迫感的钩子

跑通后用浏览器打开 out/demo_digest.html 看邮件真实效果。
"""

from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "src"))

from collector import (  # noqa: E402
    load_snapshot_from_fixtures,
    load_cached_snapshot,
    load_price_table_from_fixtures,
)
from mailer import render_digest, render_plain, render_token_expired  # noqa: E402
from mcd_adapter import MCPError  # noqa: E402
from solver import DietaryPolicy, TasteProfile, solve  # noqa: E402

OUT = "out"


def hr(title: str) -> None:
    print()
    print("=" * 64)
    print(f"  {title}")
    print("=" * 64)


def demo_a() -> None:
    """链路 A：午间推荐。"""
    hr("链路 A · 午间推荐（四层约束求解）")

    snap = load_snapshot_from_fixtures(scene="delivery")

    print(f"数据来源：fixtures 录制于 {snap.fetched_at}")
    print(f"门店：{snap.store.name if snap.store else '未知'}"
          f"（{snap.store.distance_km if snap.store else '?'} km）")
    print(f"菜单可用 {len(snap.menu)} 项"
          f"（已剔除本店不可售商品）")
    print(f"当前门店券 {len(snap.store_coupons)} 张 · 卡包券 {len(snap.my_coupons)} 张")
    print(f"门店活动：{'、'.join(snap.campaign_names) or '无'}")

    profile = TasteProfile(
        top_codes=("9900005456",),
        liked_tags={"人气经典": 10, "冷饮": 4, "小食": 6},
        favorite_store_code="1990622",
        order_count=4,
    )
    policy = DietaryPolicy(
        max_kcal=900,
        max_sodium_mg=2000,
        budget_yuan="45",
    )

    decision = solve(snap, profile, policy, load_price_table_from_fixtures())

    print()
    print("-" * 64)
    if decision.best is None:
        print("未生成建议。")
    else:
        b = decision.best
        price = f"{b.quoted_price}" if b.is_quoted else "未核价（本项目不使用估算值）"
        print(f"推荐：{b.name}")
        print(f"  价格：{price}")
        if b.coupon:
            print(f"  用券：{b.coupon.title}")
        n = b.nutrition
        print(f"  营养：热量 {n.kcal if n.kcal is not None else '未核实'} kcal"
              f" · 蛋白 {n.protein_g if n.protein_g is not None else '未核实'} g"
              f" · 钠 {n.sodium_mg if n.sodium_mg is not None else '未核实'} mg")
        print("  理由：")
        for r in b.reasons:
            print(f"    · {r}")
        for w in b.warnings:
            print(f"    ! {w}")

        print()
        print("  备选：")
        for a in decision.runners_up:
            ap = f"{a.quoted_price}" if a.is_quoted else "未核价"
            print(f"    · {a.name} · {ap}")

    print()
    print("  关键观察：")
    print("    · 「麦辣鸡腿汉堡」单点 ¥22.5，比三件套贵但可与券组合")
    print("    · 三件套 originalPrice ¥47 → currentPrice ¥33.5，套餐结构本身省 ¥13.5")
    print("    · 无糖可乐营养 2 kcal / 钠 5 mg，同价替代普通可乐（140 kcal / 钠 35 mg）")
    print("    · 薯条(中) 在本店不可售，已在适配层剔除，不进入推荐")


def demo_b() -> None:
    """链路 B：Token 失效降级。"""
    hr("链路 B · Token 失效优雅降级（本项目独有）")

    print("场景：定时任务长期运行，MCP Token 过期（官方未公布有效期，实测存在 401）。")
    print()
    print("竞品做法：静默失败，用户三天后才发现服务坏了。")
    print("本项目做法：捕获 401 → 立即发续授权邮件 → 附缓存快照。")
    print()

    cached = load_cached_snapshot()
    detail = "MCP 返回 401；原因：MCP Token 无效、已过期或未提供"
    summary = ""
    if cached:
        summary = (f"缓存快照（抓取于 {cached.fetched_at}）："
                   f"{len(cached.menu)} 项菜单 · {len(cached.my_coupons)} 张券")
    html = render_token_expired("", detail, summary)

    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, "demo_token_expired.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)

    print(f"降级邮件已生成 → {path}")
    print("打开可看到：标题 / 续授权步骤 / 错误详情 / 缓存快照说明")
    print()
    print("为什么这个能力重要：")
    print("  一次性 Skill 跑完就结束，天然不会遇到 Token 过期。")
    print("  定时任务一定会遇到。不设计降级，第 3 天就静默死掉。")
    print("  把这个断点做成被设计过的体验，是本项目区别于所有同赛项目的核心。")


def demo_c() -> None:
    """链路 C：券过期提醒。"""
    hr("链路 C · 券过期提醒（每天 20:00）")

    snap = load_snapshot_from_fixtures(scene="delivery")
    coupons = snap.store_coupons or snap.my_coupons

    print("当前门店券：")
    for c in coupons:
        value = f"面值 ¥{c.value}" if c.value is not None else "面值待核（官方未提供）"
        if c.expires_in_days is None:
            exp = "到期日未提供，不催"
        elif c.expires_in_days == 0:
            exp = "今天到期"
        else:
            exp = f"{c.expires_in_days} 天后过期"
        print(f"  · {c.title} · {value} · {exp}")

    expiring = [c for c in coupons if c.expires_in_days is not None and c.expires_in_days <= 3]
    print()
    print(f"→ {len(expiring)} 张券将触发提醒")

    profile = TasteProfile(order_count=4)
    policy = DietaryPolicy()
    decision = solve(snap, profile, policy, load_price_table_from_fixtures())

    html = render_digest(decision, snap, "基于你的 4 笔历史订单", "过期提醒")
    os.makedirs(OUT, exist_ok=True)
    path = os.path.join(OUT, "demo_digest.html")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(html)

    print()
    print("邮件预览已生成 →", path)
    print("用浏览器打开，可看到真实推送效果（配色 / 卡片 / 过期钩子 / 营养行 / 免责说明）")


def demo_d() -> None:
    """链路 D：单位陷阱演示 —— 这是最容易犯且后果最严重的坑。"""
    hr("链路 D · 单位陷阱（项目最容易踩的坑）")

    import json
    from mcd_adapter import cents_to_yuan, parse_price, yuan_str_to_money

    with open("fixtures/prices.json", encoding="utf-8") as fh:
        prices = json.load(fh)["prices"]

    raw_price = prices["9900005456:1"]["data"]["price"]
    print(f"calculate-price 返回 price = {raw_price}")
    print("  官方单位是「分」")
    print(f"  正确换算：cents_to_yuan({raw_price}) = {cents_to_yuan(raw_price)}")
    print(f"  错误换算：{raw_price} 元  ← 会把 33.5 元显示成 3350 元，差 100 倍")
    print()

    with open("fixtures/menu.json", encoding="utf-8") as fh:
        menu = json.load(fh)
    raw_menu_price = menu["data"]["meals"]["9900005456"]["currentPrice"]
    print(f"query-meals 返回 currentPrice = \"{raw_menu_price}\"")
    print("  官方单位是「元（字符串）」")
    print(f"  正确换算：yuan_str_to_money(\"{raw_menu_price}\") = {yuan_str_to_money(raw_menu_price)}")
    print()

    print("结论：两个工具的单位不一致。")
    print("本项目在 mcd_adapter.Money 层统一，业务代码永远不碰「分」。")
    print("详见 MCP_INTEGRATION.md 的「单位陷阱」章节。")


def main() -> int:
    print()
    print("麦麦饭点雷达 · mcd-meal-radar")
    print("每天饭点主动来找你 —— 基于麦当劳官方 MCP 的定时优惠与营养提醒助手")
    print()
    print("零依赖 · 免 Token · 不联网 —— 所有数据来自 fixtures/ 录制的真实 MCP 响应")

    try:
        demo_a()
        demo_b()
        demo_c()
        demo_d()
    except (MCPError, FileNotFoundError, KeyError) as exc:
        print()
        print(f"演示中断：{type(exc).__name__}: {exc}")
        print("请确认在项目根目录运行：python demo.py")
        return 1

    hr("演示完成")
    print("生成的文件：")
    for f in ("demo_digest.html", "demo_token_expired.html"):
        p = os.path.join(OUT, f)
        if os.path.exists(p):
            print(f"  {p}")
    print()
    print("下一步：")
    print("  1. 用浏览器打开 out/demo_digest.html 看邮件真实效果")
    print("  2. 接入真实数据：配置 MCD_MCP_TOKEN 后运行 scheduler.py --live --all")
    print("  3. 配置 SMTP 后运行 scheduler.py --fixture --send 实际发一封试试")
    return 0


if __name__ == "__main__":
    sys.exit(main())
