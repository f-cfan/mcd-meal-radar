"""邮件渲染层 —— 把决策结果渲染成 HTML 邮件。

为什么单独一层：定时任务的产物是**邮件**，不是终端输出。
渲染必须能离线预览（demo 要求零 Token 可跑），所以这里不依赖任何网络。

设计要点：
  · 四段固定结构：过期钩子 → 决策 → 营养 → 时效信息
  · 未核实的字段一律显式标注，不静默留白
  · 不做任何品牌对比，不含营销话术
"""

from __future__ import annotations

import html
from datetime import datetime, timezone, timedelta

from mcd_adapter import Coupon, Money, Nutrition, Snapshot
from solver import Candidate, Decision

CST = timezone(timedelta(hours=8))

# 邮件模板配色 —— 克制，无营销感
CLR = {
    "bg": "#f6f6f4",
    "card": "#ffffff",
    "ink": "#1f1f1e",
    "muted": "#6b6a66",
    "line": "#e5e3de",
    "accent": "#0f6e56",
    "warn": "#a32d2d",
    "warn_bg": "#fcebeb",
    "ok_bg": "#e1f5ee",
}


def _e(text: object) -> str:
    return html.escape(str(text))


def _fmt_money(m: Money | None) -> str:
    return "未核价" if m is None else f"¥{m.yuan:.2f}"


def _fmt_nutri(n: Nutrition) -> dict[str, str]:
    """营养字段格式化。None → 「未核实」，绝不用 0 冒充「不含」。"""
    def f(v: int | None, unit: str) -> str:
        return "未核实" if v is None else f"{v}{unit}"
    return {
        "kcal": f(n.kcal, " kcal"),
        "protein": f(n.protein_g, "g"),
        "sodium": f(n.sodium_mg, "mg"),
    }


def _expiring_coupons(coupons: list[Coupon], days: int = 3) -> list[Coupon]:
    """筛出 days 天内过期的券。expiresInDays 缺失的不算 —— 不编造天数。"""
    return [c for c in coupons
            if c.expires_in_days is not None and c.expires_in_days <= days]


def render_digest(
    decision: Decision,
    snapshot: Snapshot,
    profile_summary: str = "",
    slot: str = "午间",
) -> str:
    """渲染主推送邮件。"""
    now = datetime.now(CST)
    expiring = _expiring_coupons(snapshot.store_coupons or snapshot.my_coupons)

    rows: list[str] = []

    # 段落 ①：过期钩子（最重要，放最前）
    if expiring:
        items = "".join(
            f'<li style="margin:4px 0">'
            f'<b>{_e(c.title)}</b>'
            f'<span style="color:{CLR["muted"]}">'
            f'{" · 今天到期" if c.expires_in_days == 0 else f" · {c.expires_in_days} 天后过期"}'
            f'</span></li>'
            for c in expiring
        )
        rows.append(f"""
        <tr><td style="padding:16px 20px;background:{CLR["warn_bg"]};border-left:3px solid {CLR["warn"]}">
          <div style="font-weight:500;color:{CLR["warn"]};margin-bottom:6px">
            有 {len(expiring)} 张券即将过期
          </div>
          <ul style="margin:0;padding-left:18px;font-size:13px;color:{CLR["ink"]}">{items}</ul>
        </td></tr>""")
    else:
        rows.append(f"""
        <tr><td style="padding:14px 20px;background:{CLR["ok_bg"]};border-left:3px solid {CLR["accent"]}">
          <span style="font-size:13px;color:{CLR["ink"]}">暂无 3 天内到期的券</span>
        </td></tr>""")

    # 段落 ②③：决策 + 营养
    if decision.best:
        best = decision.best
        n = _fmt_nutri(best.nutrition)
        reason_html = "".join(f'<li style="margin:3px 0">{_e(r)}</li>' for r in best.reasons)
        warn_html = "".join(f'<li style="margin:3px 0">{_e(w)}</li>' for w in best.warnings)

        price_cell = (f'<b>{_fmt_money(best.quoted_price)}</b>' if best.is_quoted
                      else '<span style="color:' + CLR["warn"] + '">未核价</span>')

        coupon_line = ""
        if best.coupon:
            coupon_line = (f'<div style="font-size:12px;color:{CLR["muted"]};margin-top:6px">'
                           f'用券：{_e(best.coupon.title)}</div>')

        rows.append(f"""
        <tr><td style="padding:18px 20px;border-top:1px solid {CLR["line"]}">
          <div style="font-size:12px;color:{CLR["muted"]};margin-bottom:6px">{slot}建议</div>
          <div style="font-size:17px;font-weight:500;color:{CLR["ink"]};margin-bottom:4px">
            {_e(best.name)}
          </div>
          <div style="font-size:20px;color:{CLR["accent"]};margin-bottom:8px">{price_cell}</div>
          {coupon_line}
          <div style="font-size:13px;color:{CLR["muted"]};line-height:1.9">
            热量 {n["kcal"]} · 蛋白 {n["protein"]} · 钠 {n["sodium"]}
          </div>
          {f'<ul style="margin:10px 0 0;padding-left:18px;font-size:13px;color:{CLR["ink"]}">{reason_html}</ul>' if reason_html else ''}
          {f'<ul style="margin:8px 0 0;padding-left:18px;font-size:12px;color:{CLR["warn"]}">{warn_html}</ul>' if warn_html else ''}
        </td></tr>""")

        # 备选 —— 必须显示券组合，否则多条同名同价会看起来像 bug
        if decision.runners_up:
            seen: set[tuple[str, str, str]] = set()
            alt_rows: list[str] = []
            for a in decision.runners_up:
                key = (a.name, str(a.quoted_price.yuan) if a.quoted_price else "-",
                       a.coupon.coupon_id if a.coupon else "-")
                if key in seen:
                    continue
                seen.add(key)
                coupon = f'<span style="color:{CLR["accent"]}"> · {_e(a.coupon.title)}</span>' if a.coupon else ""
                alt_rows.append(
                    f'<tr><td style="padding:6px 20px;font-size:13px;color:{CLR["ink"]}">'
                    f'{_e(a.name)}{coupon}'
                    f'<span style="color:{CLR["muted"]}"> · {_fmt_money(a.quoted_price)}</span>'
                    f'</td></tr>'
                )
            if alt_rows:
                rows.append(f"""
                <tr><td style="padding:12px 20px 4px">
                  <div style="font-size:12px;color:{CLR["muted"]}">备选</div>
                  <table width="100%" cellspacing="0" cellpadding="0">{"".join(alt_rows)}</table>
                </td></tr>""")
    else:
        rows.append(f"""
        <tr><td style="padding:20px;border-top:1px solid {CLR["line"]}">
          <span style="font-size:13px;color:{CLR["muted"]}">
            未能生成建议。请检查 MCD_MCP_TOKEN 配置或稍后重试。
          </span>
        </td></tr>""")

    # 段落 ④：时效信息
    bits = []
    if snapshot.store:
        dist = f"{snapshot.store.distance_km} km" if snapshot.store.distance_km else "距离未知"
        bits.append(f"门店：{snapshot.store.name}（{dist}）")
    if snapshot.points is not None:
        bits.append(f"积分：{snapshot.points}")
    if snapshot.campaign_names:
        bits.append(f"活动：{'、'.join(snapshot.campaign_names[:2])}")
    info_html = " · ".join(_e(b) for b in bits)

    # 数据来源必须显式区分，不能用 degraded 反推：
    # 真实抓取成功时 degraded 也是 False，反推会把真实数据误标成演示数据。
    _src_label = {
        "live": "麦当劳官方 MCP 实时数据",
        "fixture": "本地 fixtures 演示数据",
        "cache": "本地缓存快照（非实时）",
    }.get(getattr(snapshot, "source", "live"), "未知来源")
    meta = f"数据来源：{_src_label} · 抓取于 {snapshot.fetched_at}"
    degrade_banner = ""
    if snapshot.degraded:
        degrade_banner = f"""
        <tr><td style="padding:10px 20px;background:#faf7ef;font-size:12px;color:{CLR["muted"]};border-top:1px solid {CLR["line"]}">
          本次为缓存/降级数据，非实时。所有金额与库存以官方页面为准。
        </td></tr>"""

    notes = ""
    if decision.notes:
        notes = ('<div style="font-size:12px;color:' + CLR["muted"] +
                 ';margin-top:8px">' + _e("；".join(decision.notes)) + '</div>')

    profile_line = (f'<div style="font-size:12px;color:{CLR["muted"]};padding:0 20px 14px">'
                    f'{_e(profile_summary)}</div>') if profile_summary else ""

    return _wrap(f"""
    <table width="100%" cellspacing="0" cellpadding="0" style="background:{CLR['bg']}">
      <tr><td align="center" style="padding:24px 12px">
        <table width="560" cellspacing="0" cellpadding="0"
               style="background:{CLR['card']};border-radius:12px;overflow:hidden">

          <tr><td style="padding:20px">
            <div style="font-size:15px;font-weight:500;color:{CLR['ink']}">麦麦饭点雷达</div>
            <div style="font-size:12px;color:{CLR['muted']};margin-top:4px">
              {now.strftime('%Y-%m-%d')} · {slot} · 基于麦当劳官方 MCP 实时数据
            </div>
          </td></tr>

          {"".join(rows)}

          <tr><td style="padding:14px 20px;border-top:1px solid {CLR['line']};font-size:12px;color:{CLR['muted']}">
            {info_html}
          </td></tr>
          {degrade_banner}
          {profile_line}

          <tr><td style="padding:14px 20px;border-top:1px solid {CLR['line']};font-size:11px;color:{CLR['muted']};line-height:1.8">
            本项目只做决策辅助，<b>不代你下单、不接触支付</b>。金额与库存均来自官方 MCP 返回，
            但仍以麦当劳官方页面为准。<br>
            {_e(meta)}
          </td></tr>

        </table>
      </td></tr>
    </table>""")


def render_token_expired(email: str, error_detail: str, cached_summary: str = "") -> str:
    """Token 失效邮件 —— 这是本项目区别于所有一次性 Skill 的关键设计。

    一次性工具不会遇到 Token 过期；定时任务必然遇到。
    把这个断点做成被设计过的体验，而不是静默故障。
    """
    cached = (f'<div style="font-size:13px;color:{CLR["muted"]};margin-top:12px">'
              f'{_e(cached_summary)}</div>') if cached_summary else ""

    return _wrap(f"""
    <table width="100%" cellspacing="0" cellpadding="0" style="background:{CLR['bg']}">
      <tr><td align="center" style="padding:24px 12px">
        <table width="560" cellspacing="0" cellpadding="0"
               style="background:{CLR['card']};border-radius:12px;overflow:hidden">
          <tr><td style="padding:20px">
            <div style="font-size:15px;font-weight:500;color:{CLR["ink"]}">麦麦饭点雷达</div>
            <div style="font-size:12px;color:{CLR["muted"]};margin-top:4px">授权失效通知</div>
          </td></tr>

          <tr><td style="padding:18px 20px;background:{CLR['warn_bg']};border-left:3px solid {CLR['warn']}">
            <div style="font-weight:500;color:{CLR['warn']};margin-bottom:8px">
              需要重新授权，未能获取实时数据
            </div>
            <div style="font-size:13px;color:{CLR['ink']};line-height:1.9">
              MCP Token 已失效。请用手机号登录
              <b>open.mcd.cn/mcp</b> → 控制台 → 激活 → 复制新 Token，
              然后更新环境变量 <b>MCD_MCP_TOKEN</b> 并重启定时任务。
            </div>
          </td></tr>

          <tr><td style="padding:14px 20px;font-size:12px;color:{CLR['muted']}">
            错误详情：{_e(error_detail)}
          </td></tr>

          {f'<tr><td style="padding:0 20px 18px">{cached}</td></tr>' if cached else ''}

          <tr><td style="padding:14px 20px;border-top:1px solid {CLR['line']};font-size:11px;color:{CLR['muted']};line-height:1.8">
            你会收到这封邮件，是因为本项目每天定时运行 —— Token 失效会被第一时间告知，
            而不是让建议静默过期。
          </td></tr>
        </table>
      </td></tr>
    </table>""")


def render_plain(decision: Decision, snapshot: Snapshot, slot: str = "午间") -> str:
    """纯文本版。用于终端演示与不支持 HTML 的客户端。"""
    lines: list[str] = []
    expiring = _expiring_coupons_safe(snapshot)
    if expiring:
        lines.append(f"[!] {len(expiring)} 张券即将过期")
        for c in expiring:
            day = "今天到期" if c.expires_in_days == 0 else f"{c.expires_in_days} 天后"
            lines.append(f"    - {c.title} · {day}")

    if decision.best:
        b = decision.best
        price = _fmt_money(b.quoted_price) if b.is_quoted else "未核价"
        lines.append("")
        lines.append(f"{slot}建议：{b.name}")
        lines.append(f"  价格：{price}")
        if b.coupon:
            lines.append(f"  用券：{b.coupon.title}")
        n = b.nutrition
        lines.append(f"  营养：热量 {n.kcal if n.kcal is not None else '未核实'} kcal"
                     f" · 蛋白 {n.protein_g if n.protein_g is not None else '未核实'}g")
        for r in b.reasons:
            lines.append(f"  · {r}")
        for w in b.warnings:
            lines.append(f"  ! {w}")
    else:
        lines.append("")
        lines.append("未能生成建议")

    if snapshot.store:
        lines.append("")
        lines.append(f"门店：{snapshot.store.name}")
    lines.append("")
    lines.append("本项目只做决策辅助，不代下单、不接触支付。")
    return "\n".join(lines)


def _expiring_coupons_safe(snapshot: Snapshot) -> list[Coupon]:
    return _expiring_coupons(snapshot.store_coupons or snapshot.my_coupons)


def _wrap(inner: str) -> str:
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>麦麦饭点雷达</title></head>
<body style="margin:0;padding:0;background:{CLR['bg']}">
{inner}
</body></html>"""
