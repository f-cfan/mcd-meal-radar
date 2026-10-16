"""中国国家法定节假日识别与视觉素材库加载模块（零第三方依赖）。

为什么单独一层
--------------
1. **推送内容按中国节日自适应风格**：在国家法定节假日（元旦、春节、清明、
   劳动节、端午、中秋、国庆）自动切换应景的节气/节日问候语、主题色板，并在求解器
   中对契合节日场景的餐品给予温和、可解释的加分。
2. **节日素材库背景图引用**：集中管理 `assets/festivals/` 下的节日插画背景与
   `manifest.json` 配置，支持本地离线相对路径与线上 GitHub Pages 公网地址自动切换。
3. **严守零依赖纪律**：不引入任何第三方农历库，通过「公历固定假期规则 +
   2025–2030 农历法定假日静态日历表 + 显式 `--festival` 覆盖开关」实现 100% 确定性识别。
"""

from __future__ import annotations

import functools
import json
import os
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
FESTIVAL_ASSET_DIR = ROOT_DIR / "assets" / "festivals"
MANIFEST_PATH = FESTIVAL_ASSET_DIR / "manifest.json"

# GitHub Pages 默认公网静态资源前缀（供真实邮件发送时在外部邮箱客户端正常渲染背景图）
DEFAULT_PUBLIC_ASSET_BASE = "https://f-cfan.github.io/mcd-meal-radar/assets/festivals"

# 7 大国家法定节假日标准顺序
STATUTORY_FESTIVAL_KEYS: tuple[str, ...] = (
    "new_year",
    "spring_festival",
    "qingming",
    "labor_day",
    "dragon_boat",
    "mid_autumn",
    "national_day",
)

# 支持中文名或简称直接指定（例如 `--festival 春节` 或 `--festival mid_autumn`）
_FESTIVAL_ALIAS_PAIRS: tuple[tuple[str, str], ...] = (
    ("new_year", "new_year"),
    ("yuandan", "new_year"),
    ("元旦", "new_year"),
    ("元旦节", "new_year"),
    ("spring_festival", "spring_festival"),
    ("spring", "spring_festival"),
    ("chunjie", "spring_festival"),
    ("春节", "spring_festival"),
    ("除夕", "spring_festival"),
    ("新春", "spring_festival"),
    ("qingming", "qingming"),
    ("清明", "qingming"),
    ("清明节", "qingming"),
    ("labor_day", "labor_day"),
    ("may_day", "labor_day"),
    ("laodong", "labor_day"),
    ("劳动节", "labor_day"),
    ("五一", "labor_day"),
    ("五一劳动节", "labor_day"),
    ("dragon_boat", "dragon_boat"),
    ("duanwu", "dragon_boat"),
    ("端午", "dragon_boat"),
    ("端午节", "dragon_boat"),
    ("mid_autumn", "mid_autumn"),
    ("zhongqiu", "mid_autumn"),
    ("中秋", "mid_autumn"),
    ("中秋节", "mid_autumn"),
    ("national_day", "national_day"),
    ("guoqing", "national_day"),
    ("国庆", "national_day"),
    ("国庆节", "national_day"),
    ("十一", "national_day"),
)

# 2025-2030 年中国农历法定节假日（春节、端午、中秋）日期区间表
# 按优先级排列：当农历中秋与国庆黄金周重叠时（如 2025-10-06、2028-10-03），优先命中中秋。
LUNAR_HOLIDAY_RANGES: tuple[tuple[str, str, str], ...] = (
    # 中秋节（农历八月十五及连休窗口）
    ("2025-10-06", "2025-10-06", "mid_autumn"),
    ("2026-09-25", "2026-09-27", "mid_autumn"),
    ("2027-09-15", "2027-09-17", "mid_autumn"),
    ("2028-10-02", "2028-10-03", "mid_autumn"),
    ("2029-09-21", "2029-09-23", "mid_autumn"),
    ("2030-09-11", "2030-09-13", "mid_autumn"),
    # 春节（农历除夕至正月初七假期窗口）
    ("2025-01-28", "2025-02-04", "spring_festival"),
    ("2026-02-15", "2026-02-23", "spring_festival"),
    ("2027-02-05", "2027-02-12", "spring_festival"),
    ("2028-01-25", "2028-02-01", "spring_festival"),
    ("2029-02-12", "2029-02-19", "spring_festival"),
    ("2030-02-02", "2030-02-09", "spring_festival"),
    # 端午节（农历五月初五及连休窗口）
    ("2025-05-31", "2025-06-02", "dragon_boat"),
    ("2026-06-19", "2026-06-21", "dragon_boat"),
    ("2027-06-08", "2027-06-10", "dragon_boat"),
    ("2028-05-27", "2028-05-29", "dragon_boat"),
    ("2029-06-15", "2029-06-17", "dragon_boat"),
    ("2030-06-04", "2030-06-06", "dragon_boat"),
)


@dataclass(frozen=True)
class FestivalTheme:
    """节日邮件视觉配色方案。"""

    page_bg: str
    card_bg: str
    accent: str
    care_bg: str
    care_border: str
    care_ink: str
    badge_bg: str
    badge_ink: str
    hero_overlay: str


@dataclass(frozen=True)
class FestivalSpec:
    """单个国家法定节假日的完整定义（风格文案 + 视觉素材 + 求解加分）。"""

    key: str
    name: str
    badge: str
    statutory_note: str
    bg_image: str
    palette: FestivalTheme
    greeting_headline: str
    greeting_body: str
    slot_greetings: dict[str, tuple[str, str]]
    boost_tags: tuple[str, ...]
    boost_keywords: tuple[str, ...]
    boost_score: int
    boost_reason: str

    def greeting_for_meal(self, meal: str) -> tuple[str, str]:
        """按当前餐次（breakfast/lunch/tea/dinner）返回节日专属 (标题, 正文)。"""
        if meal in self.slot_greetings:
            return self.slot_greetings[meal]
        return (self.greeting_headline, self.greeting_body)

    @property
    def image_path(self) -> Path:
        """返回本地素材库中对应背景图的绝对路径。"""
        return FESTIVAL_ASSET_DIR / self.bg_image


def _parse_spec(key: str, raw: dict[str, Any]) -> FestivalSpec:
    pal = raw.get("palette") or {}
    theme = FestivalTheme(
        page_bg=str(pal.get("page_bg", "#FAF5EF")),
        card_bg=str(pal.get("card_bg", "#FFFFFF")),
        accent=str(pal.get("accent", "#B83B2A")),
        care_bg=str(pal.get("care_bg", "#FDF3EE")),
        care_border=str(pal.get("care_border", "#B83B2A")),
        care_ink=str(pal.get("care_ink", "#6E1E14")),
        badge_bg=str(pal.get("badge_bg", "rgba(184, 59, 42, 0.90)")),
        badge_ink=str(pal.get("badge_ink", "#FFFFFF")),
        hero_overlay=str(
            pal.get(
                "hero_overlay",
                "linear-gradient(180deg, rgba(30,10,8,0.22) 0%, rgba(255,255,255,0.95) 100%)",
            )
        ),
    )
    slot_map: dict[str, tuple[str, str]] = {}
    for meal_key, pair in (raw.get("slot_greetings") or {}).items():
        if isinstance(pair, (list, tuple)) and len(pair) >= 2:
            slot_map[str(meal_key)] = (str(pair[0]), str(pair[1]))

    return FestivalSpec(
        key=key,
        name=str(raw.get("name", key)),
        badge=str(raw.get("badge", raw.get("name", key))),
        statutory_note=str(raw.get("statutory_note", "国家法定节假日")),
        bg_image=str(raw.get("bg_image", f"{key}.png")),
        palette=theme,
        greeting_headline=str(raw.get("greeting_headline", "")),
        greeting_body=str(raw.get("greeting_body", "")),
        slot_greetings=slot_map,
        boost_tags=tuple(str(x) for x in (raw.get("boost_tags") or ())),
        boost_keywords=tuple(str(x) for x in (raw.get("boost_keywords") or ())),
        boost_score=int(raw.get("boost_score", 8)),
        boost_reason=str(raw.get("boost_reason", "")),
    )


@functools.lru_cache(maxsize=4)
def _load_cached_library(path_str: str) -> dict[str, FestivalSpec]:
    with open(path_str, encoding="utf-8") as fh:
        data = json.load(fh)
    festivals_raw = data.get("festivals") or {}
    return {k: _parse_spec(k, v) for k, v in festivals_raw.items()}


def load_festival_library(manifest_path: Path | None = None) -> dict[str, FestivalSpec]:
    """加载 `assets/festivals/manifest.json` 节日素材库。"""
    path = manifest_path or MANIFEST_PATH
    return dict(_load_cached_library(str(path.resolve())))


def list_statutory_festivals() -> list[FestivalSpec]:
    """按固定顺序返回全部 7 个国家法定节假日定义。"""
    lib = load_festival_library()
    return [lib[k] for k in STATUTORY_FESTIVAL_KEYS if k in lib]


def get_festival(key_or_alias: str | None) -> FestivalSpec | None:
    """根据标准 key 或中文名获取节日配置，未命中返回 None。"""
    if not key_or_alias:
        return None
    alias_map = dict(_FESTIVAL_ALIAS_PAIRS)
    norm = key_or_alias.strip().lower()
    canonical = alias_map.get(norm) or alias_map.get(key_or_alias.strip())
    if not canonical:
        return None
    lib = load_festival_library()
    return lib.get(canonical)


def _coerce_date(target: date | datetime | str | None) -> date:
    if target is None:
        return date.today()
    if isinstance(target, datetime):
        return target.date()
    if isinstance(target, date):
        return target
    if isinstance(target, str):
        s = target.strip()[:10]
        return date.fromisoformat(s)
    return date.today()


def detect_festival(
    target_date: date | datetime | str | None = None,
    override_key: str | None = None,
) -> FestivalSpec | None:
    """识别给定日期所属的中国国家法定节假日。

    优先级：
      1. 显式传入的 `override_key`（例如命令行 `--festival spring_festival`）
      2. 环境变量 `MCD_FESTIVAL`（方便线上或演示一键预览节日主题）
      3. 2025–2030 农历法定节假日查表（春节、端午、中秋）
      4. 公历固定国家法定节假日规则：
         · 元旦：1月1日 ~ 1月3日
         · 清明节：4月4日 ~ 4月6日
         · 劳动节：5月1日 ~ 5月5日
         · 国庆节：10月1日 ~ 10月7日
    """
    # ⚠️ 非字符串输入要归一化 —— `python scheduler.py --festival 123`
    #    会把 int 传进来，早期版本直接调 .strip() 抛 AttributeError。
    #    这条路径属于「用户手输错了」，应当优雅降级而不是崩掉。
    explicit = override_key if override_key is not None else os.environ.get("MCD_FESTIVAL", "")
    if explicit is not None and not isinstance(explicit, str):
        explicit = str(explicit)
    explicit = explicit.strip() if explicit else ""

    if explicit.lower() not in ("", "none", "off", "0", "false"):
        spec = get_festival(explicit)
        if spec is not None:
            return spec
        # 🔴 显式指定了却认不出来 → **必须告警**。
        # 静默回退到「按日期检测」会让用户以为自己在看春节主题，
        # 结果看到的是日常样式却找不到原因 —— 这违背本项目
        # 「不静默失败」的核心纪律。
        print(f"⚠️  未知节日标识 {explicit!r}，已忽略。"
              f"可用：{'、'.join(STATUTORY_FESTIVAL_KEYS)}"
              f"（或中文名，如 春节/中秋）")
        return None

    if explicit.lower() in ("none", "off", "0", "false"):
        return None

    d = _coerce_date(target_date)
    iso = d.isoformat()

    # 1. 先查农历假期（处理中秋与国庆重叠时的优先级）
    for start_iso, end_iso, fkey in LUNAR_HOLIDAY_RANGES:
        if start_iso <= iso <= end_iso:
            return get_festival(fkey)

    # 2. 公历固定国家法定节假日
    m, day = d.month, d.day
    if m == 1 and 1 <= day <= 3:
        return get_festival("new_year")
    if m == 4 and 4 <= day <= 6:
        return get_festival("qingming")
    if m == 5 and 1 <= day <= 5:
        return get_festival("labor_day")
    if m == 10 and 1 <= day <= 7:
        return get_festival("national_day")

    return None


def resolve_festival_bg_url(
    spec: FestivalSpec,
    asset_base_url: str | None = None,
) -> str:
    """解析邮件 HTML 中引用的节日背景图 URL。

    · 若显式传入 `asset_base_url` 或设置了环境变量 `MCD_ASSET_BASE_URL`，则使用该前缀；
    · 默认使用 `../assets/festivals/<bg_image>`，使 `out/*.html` 本地离线直接打开即可显示。
    """
    base = (
        asset_base_url
        if asset_base_url is not None
        else os.environ.get("MCD_ASSET_BASE_URL", "../assets/festivals")
    ).rstrip("/")
    return f"{base}/{spec.bg_image}"
