#!/usr/bin/env python3
"""构建在线预览站点（供 GitHub Pages 部署）。

为什么需要它
------------
这个比赛**只按 GitHub Star 排名**。评委和路人大多是用手机点开链接、
快速划一眼就走 —— 不会真的 clone 仓库再跑 `python demo.py`。
把邮件效果做成**可直接打开的网页**，是当前边际收益最高的一件事。

它同时被两处使用，避免逻辑分叉：
  · 本地：生成首屏截图（`--shots`）
  · CI：部署到 GitHub Pages（默认）

用法
----
    python scripts/build_preview.py              # 只生成 out/preview/
    python scripts/build_preview.py --shots      # 额外生成 assets/shots/*.png
    python scripts/build_preview.py --index      # 额外生成 out/preview/index.html

**零依赖、零Token、不联网** —— 全部基于 fixtures，
所以 CI 里跑它不需要任何凭证。
"""

from __future__ import annotations

import argparse
import html as html_mod
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"
OUT = ROOT / "out" / "preview"
SHOTS = ROOT / "assets" / "shots"

sys.path.insert(0, str(SRC))
sys.path.insert(0, str(ROOT))

# 预览页展示哪几封邮件。key → (源文件, 展示标题, 一句话说明)
PAGES = [
    ("demo_digest.html", "午间建议",
     "推荐餐品、真实核价金额、费用构成、营养数据，附「为什么这么推」"),
    ("demo_token_expired.html", "Token 失效降级",
     "定时任务必然遇到 Token 过期 —— 这里主动告知并附续授权步骤，而不是静默失败"),
    ("care-morning.html", "早间 · 早餐还来得及",
     "09:00 档：按官方营业时段给时段问候 + 当日总览"),
    ("care-lunch.html", "午间 · 到饭点啦",
     "11:00 档：午餐建议"),
    ("care-dinner.html", "晚间 · 下午三点",
     "16:30 档：下午茶时段的关怀"),
    ("care-expiry.html", "晚间 · 到晚饭时间了",
     "20:00 档：过期提醒 + 晚餐建议"),
]

INDEX_CSS = """
:root {
  --ink: #1f1f1e; --muted: #6b6a66; --line: #e5e3de;
  --bg: #f6f6f4; --card: #ffffff; --brand: #0f6e56; --brand-soft: #e1f5ee;
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 20px 64px;
  background: var(--bg); color: var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, "PingFang SC",
               "Microsoft YaHei", "Helvetica Neue", sans-serif;
  line-height: 1.7;
}
.wrap { max-width: 1000px; margin: 0 auto; }
header { margin-bottom: 28px; }
h1 { font-size: 26px; font-weight: 700; margin: 0 0 8px; }
.sub { color: var(--muted); font-size: 14px; }
.badges { margin-top: 14px; display: flex; gap: 8px; flex-wrap: wrap; }
.badge {
  display: inline-block; padding: 4px 10px; border-radius: 999px;
  background: var(--brand-soft); color: var(--brand);
  font-size: 12px; font-weight: 600;
}
.notice {
  margin: 24px 0; padding: 14px 18px; border-radius: 10px;
  background: #fff8ee; border-left: 3px solid #BA7517;
  font-size: 13px; color: #5F5E5A;
}
.notice b { color: #8A4B0F; }
.grid { display: grid; gap: 18px; grid-template-columns: repeat(auto-fit, minmax(300px, 1fr)); }
.card {
  background: var(--card); border: 1px solid var(--line);
  border-radius: 12px; padding: 20px; display: flex; flex-direction: column;
}
.card h2 { font-size: 16px; margin: 0 0 6px; }
.card p { font-size: 13px; color: var(--muted); margin: 0 0 14px; flex: 1; }
.card .links { display: flex; gap: 10px; }
.card a {
  font-size: 13px; font-weight: 600; color: var(--brand);
  text-decoration: none; padding: 6px 12px; border-radius: 6px;
  border: 1px solid var(--brand); transition: background .15s;
}
.card a:hover { background: var(--brand-soft); }
footer { margin-top: 36px; text-align: center; font-size: 12px; color: var(--muted); }
footer a { color: var(--brand); }
"""


def run(script: str) -> bool:
    """跑一个项目脚本，失败不抛 —— 预览是增强项，不该卡住主流程。"""
    try:
        proc = subprocess.run(
            [sys.executable, script], cwd=ROOT,
            capture_output=True, text=True, timeout=300,
        )
        return proc.returncode == 0
    except Exception as exc:
        print(f"  ⚠️ {script} 执行异常：{type(exc).__name__}")
        return False


def build_sources() -> None:
    """生成全部邮件 HTML（零依赖离线数据）。"""
    print("▸ 生成邮件产物…")
    if not run("demo.py"):
        print("  ⚠️ demo.py 未成功，用已有的 out/*.html")
    # 四档关怀邮件：与线上调度走同一套代码，保证预览=真实效果
    if not run("scripts/build_care_previews.py"):
        print("  ⚠️ care 预览未成功")


def collect() -> list[tuple[str, str, str]]:
    """把产物收集到 out/preview/，返回实际存在的页面列表。"""
    OUT.mkdir(parents=True, exist_ok=True)
    found: list[tuple[str, str, str]] = []
    for fname, title, desc in PAGES:
        # 源文件可能直接在 out/ 也可能在 out/preview/
        for cand in (OUT / fname, ROOT / "out" / fname):
            if cand.exists():
                dst = OUT / fname
                if cand.resolve() != dst.resolve():
                    dst.write_bytes(cand.read_bytes())
                found.append((fname, title, desc))
                break
    return found


def write_index(pages: list[tuple[str, str, str]]) -> None:
    """生成预览首页。"""
    cards = "\n".join(
        f"""      <div class="card">
        <h2>{html_mod.escape(title)}</h2>
        <p>{html_mod.escape(desc)}</p>
        <div class="links">
          <a href="{html_mod.escape(fname)}" target="_blank" rel="noopener">打开邮件</a>
        </div>
      </div>"""
        for fname, title, desc in pages
    )
    (OUT / "index.html").write_text(f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>麦麦饭点雷达 · 邮件效果预览</title>
<style>{INDEX_CSS}</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>麦麦饭点雷达 · 每天饭点主动来找你</h1>
    <div class="sub">基于麦当劳官方 MCP 的定时优惠与营养搭配提醒助手 —— 以下是它每天发到邮箱里的真实邮件</div>
    <div class="badges">
      <span class="badge">零依赖</span>
      <span class="badge">零 Token</span>
      <span class="badge">不代下单</span>
      <span class="badge">不接触支付</span>
      <span class="badge">金额只来自 calculate-price</span>
    </div>
  </header>

  <div class="notice">
    <b>这批邮件由 fixtures 离线生成</b>，不需要 MCP Token 也不联网。
    里面出现的餐品、价格、门店、活动均为 2026-10-15 真实 MCP 调用的返回内容
    （地址已脱敏），权重与线上一致。
  </div>

  <div class="grid">
{cards}
  </div>

  <footer>
    <a href="https://github.com/f-cfan/mcd-meal-radar">查看源码与实测踩坑记录 →</a>
  </footer>
</div>
</body>
</html>
""", encoding="utf-8")
    print(f"  ✅ {OUT / 'index.html'}")


def make_shots(pages: list[tuple[str, str, str]]) -> None:
    """把邮件渲染成 PNG 供 README 引用（需要本机 Chrome）。"""
    chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    if not os.path.exists(chrome):
        print("  ⚠️ 未找到 Chrome，跳过截图")
        return
    import tempfile
    SHOTS.mkdir(parents=True, exist_ok=True)
    for fname, title, _ in pages:
        src = OUT / fname
        if not src.exists():
            continue
        with tempfile.TemporaryDirectory() as tmp:
            png = Path(tmp) / (Path(fname).stem + ".png")
            try:
                subprocess.run(
                    [chrome, "--headless", "--no-sandbox", "--disable-gpu",
                     "--disable-dev-shm-usage", f"--user-data-dir={tmp}/ud",
                     f"--screenshot={png}", "--window-size=640,1120",
                     src.resolve().as_uri()],
                    capture_output=True, timeout=90,
                )
            except Exception:
                continue
            if png.exists():
                dst = SHOTS / f"{Path(fname).stem}.png"
                dst.write_bytes(png.read_bytes())
                print(f"  ✅ {dst.name}（{title}）")


def main() -> int:
    ap = argparse.ArgumentParser(description="构建麦麦饭点雷达在线预览")
    ap.add_argument("--index", action="store_true", help="生成预览首页")
    ap.add_argument("--shots", action="store_true", help="额外渲染 PNG 截图")
    args = ap.parse_args()

    build_sources()
    pages = collect()
    if not pages:
        print("❌ 没有可用的邮件产物")
        return 1
    print(f"▸ 收集到 {len(pages)} 封邮件")

    if args.index or True:      # 首页是 Pages 的入口，始终生成
        write_index(pages)

    if args.shots:
        print("▸ 渲染截图…")
        make_shots(pages)

    print(f"✅ 预览就绪：{(OUT / 'index.html')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
