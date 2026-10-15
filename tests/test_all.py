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
    yuan_str_to_money,
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
    def test_missing_field_stays_none(self):
        """官方数据缺 calcium 时必须为 None，不能变 0。"""
        table = parse_nutrition_list(fx("nutrition"))
        self.assertIsNone(table["1440"].calcium_mg)
        self.assertEqual(table["9900005456"].kcal, 742)

    def test_zero_is_preserved_as_real_zero(self):
        """真实的 0（如无糖可乐碳水 0）必须保留为 0，不能与 None 混淆。"""
        table = parse_nutrition_list(fx("nutrition"))
        self.assertEqual(table["9900008752"].carb_g, 0)
        self.assertEqual(table["9900008752"].kcal, 2)


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


if __name__ == "__main__":
    unittest.main(verbosity=2)
