#!/usr/bin/env python3
"""生成四档关怀邮件预览（零依赖离线）。

⚠️ 设计要点：**复用 scheduler 的真实求解与关怀逻辑**，不在这里另写一套。
预览与线上不一致是最容易翻车的地方 —— 之前首屏截图的矛盾
（关怀说「有 2 张券今天过期」而下方写「暂无券」）就是手工注入信号造成的。

本脚本只做一件事：**把时间基准固定到各档位的绑定时刻**。
`run_slot` 已经用 `slot.hour` 算餐次了，这里直接复用它。

用法：python scripts/build_care_previews.py
"""

from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

import scheduler as sched  # noqa: E402


def main() -> int:
    # 固定一个日期，确保离线渲染可复现（fixtures 里的数据日期）
    base = datetime(2026, 10, 16, tzinfo=sched.CST)

    for slot in sched.SLOTS:
        # 关键：now 传 None → run_slot 内部用 slot.hour 算时段，
        # 而漏餐等信号只依赖日期部分，与真实运行一致。
        try:
            res = sched.run_slot(slot, mode="fixture", outdir="out/preview")
        except TypeError:
            res = sched.run_slot(slot, mode="fixture", outdir="out/preview",
                                 now=base)
        # 重命名到 care-<key>.html，与 build_preview 的清单对应
        src = Path("out/preview") / f"{slot.key}.html"
        dst = Path("out/preview") / f"care-{slot.key}.html"
        if src.exists():
            if dst.exists():
                dst.unlink()
            src.rename(dst)
        print(f"  {'✅' if dst.exists() else '❌'} care-{slot.key}.html"
              f"（{slot.label}）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
