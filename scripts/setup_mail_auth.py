#!/usr/bin/env python3
"""推送通道配置指引与自检。

两条通道
--------
1. **Agent Mail**（推荐）—— QQ 邮箱团队的 Agent 专属邮箱服务
   免授权码、扫码即用、与个人邮箱隔离。
   WorkBuddy 用户：首页「更多 - 我的邮箱」直接启用。

2. **SMTP**（备选）—— 自建邮件通道
   需授权码，而授权码只能由腾讯官方流程签发：
       mail.qq.com → 设置 → 账号与安全 → 开启 IMAP/SMTP
       → 手机短信验证 → 生成 16 位授权码
   **短信验证必须用户本人操作，任何第三方工具都无法代替。**

用法：
    python scripts/setup_mail_auth.py          # 打开页面 + 检查两条通道
    python scripts/setup_mail_auth.py --check  # 只检查不打开页面
"""

from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENV_PATH = os.path.join(ROOT, ".env")

MAIL_URL = "https://mail.qq.com/mail/setting"
AGENT_MAIL_URL = "https://agent.qq.com"

# 16 位授权码通常是小写字母 + 数字混合；这里只做宽松提示，不做严格校验
AUTH_CODE_RE = re.compile(r"^[A-Za-z0-9]{16}$")


def open_browser(url: str) -> bool:
    if sys.platform == "darwin":
        cmd = ["open", url]
    elif sys.platform.startswith("win"):
        cmd = ["start", "", url]
    else:
        cmd = ["xdg-open", url]
    try:
        subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True
    except Exception:
        return False


def read_env(path: str = ENV_PATH) -> dict[str, str]:
    env: dict[str, str] = {}
    if not os.path.exists(path):
        return env
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def agent_mail_available() -> bool:
    """检测 Agent Mail CLI 是否已安装。"""
    from shutil import which
    return which("agently-cli") is not None


def check() -> int:
    env = read_env()
    # 系统环境变量优先（launchd / cron 场景）
    def get(key: str, default: str = "") -> str:
        return os.environ.get(key) or env.get(key, default)

    print()
    print("=" * 58)
    print("  推送通道配置自检")
    print("=" * 58)

    # ---- 通道一：Agent Mail ----
    agent_cli = agent_mail_available()
    channel = os.environ.get("MAIL_CHANNEL") or env.get("MAIL_CHANNEL", "auto")
    print()
    print("  通道一 · Agent Mail（推荐）")
    print(f"    {'✅' if agent_cli else 'ℹ️ '} CLI        "
          f"{'已安装' if agent_cli else '未安装（WorkBuddy 内置能力仍可用）'}")
    print(f"    {'✅' if channel != 'smtp' else '⚠️ '} MAIL_CHANNEL  {channel}")
    if not agent_cli:
        print("       → 启用方式：WorkBuddy 首页「更多 - 我的邮箱」，扫码即可")
        print(f"       → 或访问 {AGENT_MAIL_URL} 查看你的 Agent 邮箱")

    user = get("SMTP_USER")
    password = get("SMTP_PASSWORD")
    to = get("RADAR_TO") or user
    host = get("SMTP_HOST") or "smtp.qq.com"
    port = get("SMTP_PORT") or "465"

    ok = True

    def line(label: str, value: str, passed: bool, hint: str = "") -> None:
        mark = "✅" if passed else "❌"
        shown = value if value else "（未配置）"
        if label == "授权码" and value:
            shown = f"{value[:4]}…{value[-4:]}（已脱敏）"
        print(f"  {mark} {label:<10} {shown}")
        if not passed and hint:
            print(f"     → {hint}")

    print()
    print("  通道二 · SMTP（备选）")
    line("发件邮箱", user, bool(user), "在 .env 里设置 SMTP_USER")
    line("收件邮箱", to, bool(to), "在 .env 里设置 RADAR_TO（缺省与发件相同）")
    line("授权码", password, bool(password),
         "按官方流程获取：mail.qq.com → 设置 → 账号与安全 → 开启 IMAP/SMTP")

    if password:
        valid = bool(AUTH_CODE_RE.match(password))
        if valid:
            print(f"     → 格式符合 16 位授权码特征")
        else:
            print(f"     ⚠️ 长度/字符与典型授权码不符，请核对（长度 {len(password)}）")

    print(f"  {'✅' if host else '❌'} {'SMTP 服务器':<10} {host}:{port}")
    print(f"  {'✅' if get('SMTP_USE_SSL', '1') == '1' else '⚠️'} {'加密方式':<10} "
          f"{'SSL/TLS 直连 (465)' if get('SMTP_USE_SSL', '1') == '1' else 'STARTTLS (587)'}")

    print()
    if ok and password:
        print("SMTP 配置完整。测试发送：")
        print("    python scheduler.py --fixture --now lunch --send")
        print()
        print("提示：在 WorkBuddy 中运行时，直接说「帮我发一封测试邮件」即可走内置 Agent Mail，")
        print("      无需任何授权码配置。")
        return 0

    print("SMTP 未配置完整。这是正常的 —— 你在 WorkBuddy 中运行时不需要它。")
    print()
    print("推荐路径（二选一）：")
    print("  A. 用 Agent Mail：WorkBuddy 首页「更多 - 我的邮箱」→ 扫码启用，零配置")
    print(f"     你的 Agent 邮箱可在 {AGENT_MAIL_URL} 查看")
    print("  B. 用 SMTP：按上方路径获取 16 位授权码，填入 .env 的 SMTP_PASSWORD")
    print()
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description="授权码指引与配置自检")
    ap.add_argument("--check", action="store_true", help="只检查配置，不打开浏览器")
    args = ap.parse_args()

    if not args.check:
        print()
        print("正在打开 QQ 邮箱授权码页面...")
        print(f"  {MAIL_URL}")
        print()
        print("页面里的路径：")
        print("  设置 → 账号与安全 → 找到「POP3/IMAP/SMTP/Exchange/CardDAV服务」")
        print("  → 点「开启」→ 按提示发短信验证 → 生成 16 位授权码")
        print()
        print("⚠️ 授权码只能在官方页面生成，任何第三方工具都无法代替。这是账号安全设计。")
        print("   关闭页面后授权码不再显示，请当场复制保存。")
        print()
        if open_browser(MAIL_URL):
            print("已在浏览器中打开。若未自动打开，请手动访问上面的地址。")
        else:
            print("自动打开失败，请手动访问上面的地址。")

    return check()


if __name__ == "__main__":
    sys.exit(main())
