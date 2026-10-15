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

from collector import get_snapshot  # noqa: E402
from mail import describe_channels, push  # noqa: E402
from mailer import render_digest, render_plain, render_token_expired  # noqa: E402
from mcd_adapter import MCPError  # noqa: E402
from solver import DietaryPolicy, Decision, TasteProfile, load_profile, solve  # noqa: E402

CST = timezone(timedelta(hours=8))

PROFILE_PATH = "data/profile.json"


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


def profile_summary(profile: TasteProfile) -> str:
    """画像摘要。显示在邮件底部 —— 让用户知道建议基于什么。"""
    bits = []
    if profile.order_count:
        bits.append(f"基于你的 {profile.order_count} 笔历史订单")
    if profile.top_codes:
        bits.append(f"常点 {len(profile.top_codes)} 款")
    return " · ".join(bits) if bits else "首次运行，尚未建立偏好画像"


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

    # 1) 抓数据
    snap = get_snapshot(mode=mode, scene="delivery", auto_claim=slot.auto_claim)

    token_dead = mode == "live" and snap.degraded and "Token" in (snap.degrade_reason or "")

    # 2) 求解
    if token_dead:
        decision: Decision = Decision(None, [], ["实时数据"], ["Token 失效"])
    else:
        profile = load_profile(PROFILE_PATH)
        decision = solve(snap, profile, load_policy())

    # 3) 渲染
    if token_dead:
        subject = "【麦麦饭点雷达】需要重新授权"
        html = render_token_expired("", snap.degrade_reason or "Token 失效")
        text = "MCP Token 已失效，请到 open.mcd.cn/mcp 重新获取并更新 MCD_MCP_TOKEN。"
    else:
        subject = f"【麦麦饭点雷达】{slot.label}建议 · {datetime.now(CST):%m-%d}"
        html = render_digest(decision, snap, profile_summary(load_profile(PROFILE_PATH)), slot.label)
        text = render_plain(decision, snap, slot.label)

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
