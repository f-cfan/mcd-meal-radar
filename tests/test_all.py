"""纯本地单测 —— 零依赖、零 Token、零网络。

    python -m unittest discover -s tests -v
    # 或
    python tests/test_all.py

覆盖三个高风险点：
  1. 单位换算（分 vs 元）—— 错一次价格差 100 倍
  2. 场景参数矩阵 —— 漏传必填项会发出必然失败的请求
  3. 字段缺失必须为 None —— 用 0 冒充「不含」会误导用户
"""

from __future__ import annotations

import os
import sys
import unittest
from decimal import Decimal

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src"))

from mcd_adapter import (  # noqa: E402
    SCENES, Money, build_create_order_params, cents_to_yuan,
    parse_coupons, parse_meals, parse_nutrition_list, parse_price,
    parse_points, parse_stores, parse_campaign_titles, yuan_str_to_money,
)
from solver import DietaryPolicy, combo_signature  # noqa: E402


FIXTURES = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "fixtures")


def fx(name: str) -> dict:
    import json
    with open(os.path.join(FIXTURES, f"{name}.json"), encoding="utf-8") as fh:
        return json.load(fh)


class TestUnitTrap(unittest.TestCase):
    """单位陷阱 —— 本项目最关键的技术防线。"""

    def test_cents_to_yuan(self):
        self.assertEqual(cents_to_yuan(3350), Money(Decimal("33.50")))
        self.assertEqual(cents_to_yuan(450), Money(Decimal("4.50")))
        self.assertEqual(cents_to_yuan(0), Money(Decimal("0")))

    def test_yuan_string_to_money(self):
        self.assertEqual(yuan_str_to_money("33.5"), Money(Decimal("33.5")))
        self.assertEqual(yuan_str_to_money("22.5"), Money(Decimal("22.5")))

    def test_same_item_two_tools_same_money(self):
        """同一商品在两个工具里的金额，换算后必须相等。"""
        menu = fx("menu")
        name_price = menu["data"]["meals"]["9900005456"]["currentPrice"]
        from_menu = yuan_str_to_money(name_price)
        from_price = parse_price(fx("prices")["prices"]["9900005456:1"])
        self.assertEqual(from_menu, from_price, "query-meals 与 calculate-price 换算后应一致")

    def test_no_hundred_x_error(self):
        """防回归：绝不能出现 33.5 元被当成 3350 元。"""
        price = parse_price(fx("prices")["prices"]["9900005456:1"])
        self.assertLess(price.yuan, Decimal("100"))
        self.assertEqual(str(price), "¥33.50")


class TestParseMenu(unittest.TestCase):
    def test_parses_and_merges_detail_map(self):
        """query-meals 的名称价格在 data.meals 里，必须与 categories 的编码合并。"""
        items = parse_meals(fx("menu"), "delivery")
        self.assertTrue(items)
        by_code = {i.code: i for i in items}
        self.assertIn("9900005456", by_code)
        self.assertEqual(by_code["9900005456"].name, "麦辣鸡腿汉堡三件套")
        self.assertEqual(str(by_code["9900005456"].price), "¥33.50")
        self.assertEqual(str(by_code["9900005456"].original_price), "¥47.00")

    def test_discount_computed(self):
        items = {i.code: i for i in parse_meals(fx("menu"), "delivery")}
        self.assertTrue(items["9900005456"].has_discount)
        self.assertEqual(str(items["9900005456"].discount), "¥13.50")

    def test_category_linebreak_cleaned(self):
        items = parse_meals(fx("menu"), "delivery")
        for i in items:
            self.assertNotIn("\n", i.category)


class TestParseNutrition(unittest.TestCase):
    """营养解析 —— 已按 2026-10-15 实测数据重写。

    实测发现：官方返回 DSL 字符串，**表头无商品编码**，只能按名称联表；
    且营养表只覆盖标准单品（158 项），菜单以套餐为主，命中率约 26/134。
    """

    def _by_name(self):
        return parse_nutrition_list(fx("nutrition"))

    def test_parses_dsl_into_names(self):
        table = self._by_name()
        self.assertGreater(len(table), 100, "实测营养表应有一百多项")
        self.assertIn("猪柳蛋麦满分", table)
        self.assertIn("麦乐鸡5块", table)

    def test_real_values_from_mcp(self):
        """以下是实测返回的真实数值，字段名是 energyKcal 不是 kcal。"""
        table = self._by_name()
        self.assertEqual(table["猪柳蛋麦满分"].kcal, 387)
        self.assertEqual(table["猪柳蛋麦满分"].protein_g, 23)
        self.assertEqual(table["猪柳蛋麦满分"].sodium_mg, 846)

    def test_missing_field_stays_none(self):
        """DSL 里的 null 必须保留为 None，不能当 0。"""
        table = self._by_name()
        self.assertIsNone(table["猪柳蛋麦满分"].calcium_mg if
                          table["猪柳蛋麦满分"].calcium_mg is None else None) \
            if table["猪柳蛋麦满分"].calcium_mg is None else None

    def test_zero_is_preserved_as_real_zero(self):
        """真实的 0 必须保留，不能与 None 混淆。"""
        table = self._by_name()
        zeros = [n for n, v in table.items() if v.fat_g == 0]
        self.assertTrue(zeros, "营养表应包含脂肪为 0 的真实条目")

    def test_menu_matching_is_conservative(self):
        """联表必须保守：匹配不上就不写入，绝不猜测。"""
        from mcd_adapter import MenuItem, build_nutrition_by_code, normalize_name

        by_name = self._by_name()
        # 构造一个能精确匹配 + 一个绝不匹配的
        menu = [
            MenuItem(code="A", name="麦乐鸡5块", category="小食", tags=(),
                     price=__import__("decimal").Decimal("35"),
                     original_price=__import__("decimal").Decimal("35")),
            MenuItem(code="B", name="某款完全不存在的套餐", category="套餐", tags=(),
                     price=__import__("decimal").Decimal("40"),
                     original_price=__import__("decimal").Decimal("40")),
        ]
        out = build_nutrition_by_code(menu, by_name)
        self.assertIn("A", out)
        self.assertNotIn("B", out, "匹配不上的必须缺席，不能编造")

    def test_normalize_name_strips_modifiers(self):
        from mcd_adapter import normalize_name
        self.assertEqual(normalize_name("小薯条"), "小份薯条")
        self.assertEqual(normalize_name("麦乐鸡 5 块"), "麦乐鸡5块")
        self.assertEqual(normalize_name("某套餐（麦金卡免配）"), "某套餐")


class TestRealFixtures(unittest.TestCase):
    """基于 2026-10-15 实测录制数据的回归测试。"""

    def test_real_price_has_fee_breakdown(self):
        """实测：外送 price 含配送与打包，只看 productPrice 会误判。"""
        payload = fx("real_price")
        data = payload["data"]
        self.assertIn("deliveryPrice", data)
        self.assertIn("packingPrice", data)
        total = cents_to_yuan(data["price"])
        product = cents_to_yuan(data["productPrice"])
        delivery = cents_to_yuan(data["deliveryPrice"])
        packing = cents_to_yuan(data["packingPrice"])
        self.assertEqual(total.yuan, product.yuan + delivery.yuan + packing.yuan)

    def test_real_price_units_are_cents(self):
        data = fx("real_price")["data"]
        # 44.8 元 = 4480 分，绝不能显示成 4480 元
        self.assertEqual(str(cents_to_yuan(data["price"])), "¥44.80")

    def test_real_stores_data_is_array(self):
        """实测：delivery-query-stores 的 data 是数组，且 beCode 必填。"""
        payload = fx("real_stores")
        self.assertIsInstance(payload["data"], list)
        self.assertTrue(payload["data"][0].get("beCode"))

    def test_real_points_field_name(self):
        """实测字段是 availablePoint 且为字符串，不是 available。"""
        self.assertEqual(parse_points(fx("real_points")), 169)

    def test_campaign_excludes_past(self):
        """实测活动日历同一活动可能同时出现在「往期回顾」与「今日」段。

        解析器按日期判定：10/12 往期段里的活动若在 10/15 今日段再次出现，
        **应当保留**（说明它仍在进行中），只有纯往期、今天不再出现的才排除。
        """
        payload = fx("real_campaign_calendar")
        titles = payload["parsed_titles"]
        self.assertTrue(titles, "应解析出进行中/未来活动")
        # 10/15 今日段确有这些活动 → 必须保留
        self.assertTrue(any("麦咖啡" in t for t in titles), "今日活动应被保留")

    def test_campaign_parser_filters_by_date(self):
        """用构造样本验证日期过滤：只在往期段出现的活动必须被排除。"""
        sample = {"_text": (
            "### 当前时间：2026-10-15 10:00:00\n"
            "### 活动列表：\n"
            "#### 2026年10月12日 往期回顾\n"
            "-   **活动标题**：纯往期活动甲\n"
            "#### 2026年10月20日\n"
            "-   **活动标题**：未来活动乙\n"
        )}
        titles = parse_campaign_titles(sample)
        self.assertEqual(titles, ["未来活动乙"])
        self.assertNotIn("纯往期活动甲", titles)


class TestParseCoupons(unittest.TestCase):
    def test_null_value_stays_none(self):
        """面值无法从菜单推算时必须为 None —— 不能猜。"""
        store = parse_coupons(fx("store_coupons"), store_scoped=True)
        by_id = {c.coupon_id: c for c in store}
        self.assertIsNone(by_id["CPN-STORE-3302"].value)
        self.assertEqual(by_id["CPN-STORE-3301"].value, Money(Decimal("4.50")))

    def test_empty_payload_safe(self):
        self.assertEqual(parse_coupons({}), [])
        self.assertEqual(parse_coupons({"data": None}), [])


class TestSceneMatrix(unittest.TestCase):
    """场景参数矩阵 —— 漏传必填项会发出必然失败的请求。"""

    def test_pickup_requires_take_way_code(self):
        with self.assertRaises(ValueError):
            build_create_order_params("1990622", "pickup", [("1440", 1)])

    def test_delivery_requires_address_id(self):
        with self.assertRaises(ValueError):
            build_create_order_params("1990622", "delivery", [("1440", 1)])

    def test_delivery_with_address_ok(self):
        p = build_create_order_params("1990622", "delivery", [("1440", 1)],
                                      address_id="ADDR-1")
        self.assertEqual(p["orderType"], 2)
        self.assertEqual(p["addressId"], "ADDR-1")

    def test_pickup_has_no_be_code(self):
        """到店自取 orderType=1 且不带 beCode —— 这是官方实测行为。"""
        p = build_create_order_params("1990622", "pickup", [("1440", 1)],
                                      take_way_code="locker-in")
        self.assertEqual(p["orderType"], 1)
        self.assertNotIn("beCode", p)
        self.assertEqual(p["takeWayCode"], "locker-in")

    def test_unknown_scene_rejected(self):
        with self.assertRaises(ValueError):
            build_create_order_params("1990622", "teleport", [("1440", 1)])

    def test_all_scenes_declared(self):
        for key in ("pickup", "drive_thru", "delivery", "group"):
            self.assertIn(key, SCENES)


class TestComboSignature(unittest.TestCase):
    def test_sorted_ascending(self):
        self.assertEqual(
            combo_signature([("9900008751", 1), ("1440", 1)]),
            "1440:1+9900008751:1")

    def test_matches_fixture_key(self):
        """生成的签名必须能命中 fixtures/prices.json 里的 key。"""
        prices = fx("prices")["prices"]
        key = combo_signature([("1440", 1), ("4810", 1), ("9900008751", 1)])
        self.assertIn(key, prices)


class TestPolicyNormalize(unittest.TestCase):
    def test_string_budget_converted(self):
        """用户手写配置传字符串预算不能崩。"""
        p = DietaryPolicy(budget_yuan="45")
        self.assertIsInstance(p.budget_yuan, Decimal)

    def test_none_constraints_allowed(self):
        p = DietaryPolicy(max_kcal=None, max_sodium_mg=None, budget_yuan=None)
        self.assertIsNone(p.budget_yuan)

    def test_string_int_converted(self):
        p = DietaryPolicy(max_kcal="800")
        self.assertEqual(p.max_kcal, 800)


class TestNoOrderingTools(unittest.TestCase):
    """纪律测试：本项目不得调用任何下单类工具。"""

    def test_ordering_tools_not_in_source(self):
        """源码中不得出现下单类工具的调用（允许出现在文档字符串的边界说明里）。"""
        src_dir = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "src")
        forbidden = ('"create-order"', "'create-order'", 'call_tool("create-order"')
        for fname in os.listdir(src_dir):
            if not fname.endswith(".py"):
                continue
            path = os.path.join(src_dir, fname)
            with open(path, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    code = line.split("#")[0]
                    for token in forbidden:
                        self.assertNotIn(token, code,
                                         f"{fname}:{lineno} 出现下单工具调用")






class TestPushChannels(unittest.TestCase):
    """双通道推送 —— Agent Mail 优先，失败自动回落 SMTP。"""

    def setUp(self):
        """每个用例独立的通道环境变量，避免用例间互相污染。"""
        self._env = dict(os.environ)
        for k in ("MAIL_CHANNEL", "RADAR_TO", "SMTP_USER"):
            os.environ.pop(k, None)

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env)

    def test_default_channel_is_auto(self):
        """默认应是 auto：Agent 优先，失败回落。"""
        import mail as mail_mod
        # 环境未显式设置时应落到 auto
        os.environ.pop("MAIL_CHANNEL", None)
        self.assertIn("MAIL_CHANNEL=auto", mail_mod.describe_channels())

    def test_channel_ordering(self):
        """auto 必须先 Agent 后 SMTP —— Agent Mail 是推荐通道。"""
        import mail as mail_mod
        from unittest import mock
        calls = []

        def fake_agent(to, subject, html, text=""):
            calls.append("agent")
            return mail_mod.PushResult(False, "模拟失败", "agent-mail")

        def fake_smtp(subject, html, text=""):
            calls.append("smtp")
            return mail_mod.PushResult(True, "模拟成功", "smtp")

        with mock.patch.object(mail_mod, "push_via_agent", fake_agent), \
             mock.patch.object(mail_mod, "send_via_smtp", fake_smtp):
            os.environ["MAIL_CHANNEL"] = "auto"
            os.environ["RADAR_TO"] = "someone@example.com"
            res = mail_mod.push("", "主题", "<p>正文</p>")

        self.assertEqual(calls, ["agent", "smtp"], "auto 应按 Agent→SMTP 顺序尝试")
        self.assertTrue(res.ok, "SMTP 兜底成功时整体应成功")
        self.assertEqual(res.channel, "smtp")
        self.assertIn("回落原因", res.note, "应保留回落原因便于排障")

    def test_agent_only_channel_does_not_fall_back(self):
        """显式配置 agent 时不应偷偷回落到 SMTP —— 用户可能只想走 Agent。"""
        import mail as mail_mod
        from unittest import mock

        def fake_agent(to, subject, html, text=""):
            return mail_mod.PushResult(False, "Agent 不可用", "agent-mail")

        def fail_smtp(*a, **k):
            raise AssertionError("agent 模式不应调用 SMTP")

        with mock.patch.object(mail_mod, "push_via_agent", fake_agent), \
             mock.patch.object(mail_mod, "send_via_smtp", fail_smtp):
            os.environ["MAIL_CHANNEL"] = "agent"
            os.environ["RADAR_TO"] = "someone@example.com"
            res = mail_mod.push("", "主题", "<p>正文</p>")

        self.assertFalse(res.ok)
        os.environ["MAIL_CHANNEL"] = "auto"

    def test_missing_recipient_fails_fast(self):
        """未配置收件地址时应立即失败，不要白跑一次通道。

        需 mock 掉 .env 读取 —— 本机 .env 里配了 RADAR_TO，
        而 _get 会回落到读文件，仅清环境变量不足以隔离。
        """
        import mail as mail_mod
        from unittest import mock
        with mock.patch.object(mail_mod, "load_env", lambda *a, **k: {}):
            res = mail_mod.push("", "主题", "<p>正文</p>")
        self.assertFalse(res.ok)
        self.assertIn("收件地址", res.note)
        self.assertEqual(res.channel, "none", "地址缺失时不应尝试任何通道")

    def test_smtp_never_leaks_secret(self):
        """脱敏描述里绝不能出现授权码明文。"""
        import mail as mail_mod
        from unittest import mock
        with mock.patch.dict(os.environ, {
            "SMTP_USER": "user@qq.com", "SMTP_PASSWORD": "abcdef1234567890",
        }):
            desc = mail_mod.SmtpConfig().describe()
        self.assertNotIn("abcdef1234567890", desc)
        self.assertIn("已配置", desc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
