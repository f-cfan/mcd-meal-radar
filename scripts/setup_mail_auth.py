#!/usr/bin/env python3
"""授权码生成指引 + 配置自检。

为什么不能自动获取授权码
------------------------
SMTP 授权码是 QQ 邮箱的**账号安全凭证**，只能由腾讯官方流程签发：
    登录 mail.qq.com → 设置 → 账号与安全 → 开启 IMAP/SMTP
    → 手机短信验证 → 生成 16 位授权码

任何第三方工具都无法代替你完成这个流程，这是账号安全设计，
不是技术限制。本脚本只做两件事：

  1. 打开官方授权码页面（省去手动找入口）
  2. 检查你填的授权码是否已配置、以及格式是否合理

用法：
    python scripts/setup_mail_auth.py          # 打开页面 + 检查配置
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


def check() -> int:
    env = read_env()
    # 系统环境变量优先（launchd / cron 场景）
    def get(key: str, default: str = "") -> str:
        return os.environ.get(key) or env.get(key, default)

    print()
    print("=" * 58)
    print("  SMTP 配置自检")
    print("=" * 58)

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
        print("配置看起来完整。测试发送：")
        print("    python scheduler.py --fixture --now lunch --send")
    else:
        print("配置未完成。请先获取授权码（见上方步骤），填入 .env 后重跑本脚本。")
    print()
    return 0 if (user and password) else 1


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
