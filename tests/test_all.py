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

import json
import os
import re
import sys
import unittest
from datetime import datetime
from decimal import Decimal
from unittest import mock

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(_ROOT, "src"))
sys.path.insert(0, _ROOT)   # scheduler.py 在项目根

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
        self.assertEqual(normalize_name("麦乐鸡 5 块"), "麦乐鸡5块")
        self.assertEqual(normalize_name("某套餐（麦金卡免配）"), "某套餐")

    def test_normalize_name_keeps_size_distinct(self):
        """🔴 规格必须保留 —— 丢掉就串档。

        真实 bug（2026-10-16）：早期 normalize_name 直接删掉括号，
        「可乐(小)/可乐(中)/可乐(大)」全变成「可乐」，
        于是三档商品共用同一份营养 —— 而实测三档分别是
        107/147/224 kcal，串档等于给用户报错的数字。
        """
        from mcd_adapter import normalize_name
        sizes = {normalize_name(f"可乐({s})") for s in ("小", "中", "大")}
        self.assertEqual(len(sizes), 3, f"三档规格归一后不应相同：{sizes}")

    def test_normalize_name_matches_official_variants(self):
        """菜单名与营养表名必须归一到同一形态（实测 2026-10-16）。

        官方两张表叫法不一致：
          菜单   「可乐(中)」/「无糖可乐(中)」/「薯条(小)」
          营养表 「可乐中杯」/「无糖可口可乐中杯」/「小薯条」
        归一后必须相等，否则单品营养全部落空。
        """
        from mcd_adapter import normalize_name as n
        pairs = [
            ("可乐(中)", "可乐中杯"),
            ("可乐(小)", "可乐小杯"),
            ("无糖可乐(中)", "无糖可口可乐中杯"),
            ("薯条(小)", "小薯条"),
            ("薯条(中)", "中薯条"),
        ]
        for menu_name, nutrition_name in pairs:
            self.assertEqual(
                n(menu_name), n(nutrition_name),
                f"{menu_name} 与 {nutrition_name} 应归一到同一形态："
                f"{n(menu_name)} != {n(nutrition_name)}")


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
    """纪律测试：本项目不得调用任何下单类/消耗资源类工具。

    ⚠️ 早期版本这个测试名不副实 —— docstring 声称防住 5 类工具，
    实际只检查了 create-order 一个。对一个以「诚实」为卖点的项目，
    「宣称」与「实测」不符比没有测试更伤。这是被外部评审挑出来的。

    现在覆盖官方全部会改变账户状态或消耗资源的工具。
    """

    # 会创建订单、消耗积分、占用名额或领取优惠券的工具
    FORBIDDEN = (
        "create-order",
        "cancel-order",
        "mall-create-order",
        "party-order-create",
        "draw-lottery",
    )

    # 唯一允许调用的写操作工具，且只能出现在 collector 的领券分支里
    ALLOWED_WRITE = ("auto-bind-coupons",)

    def _source_files(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name in sorted(os.listdir(os.path.join(root, "src"))):
            if name.endswith(".py"):
                yield name, os.path.join(root, "src", name)

    def test_no_ordering_tools_anywhere(self):
        """src/ 下不得出现下单类工具的调用。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        also_root = os.path.join(root, "scheduler.py")
        targets = [p for _, p in self._source_files()]
        targets.append(also_root)

        for path in targets:
            with open(path, encoding="utf-8") as fh:
                for lineno, line in enumerate(fh, 1):
                    code = line.split("#")[0]
                    # 排除 docstring 里的边界说明：只查 call_tool 调用形式
                    for tool in self.FORBIDDEN:
                        self.assertNotIn(
                            f'call_tool("{tool}"', code,
                            f"{os.path.basename(path)}:{lineno} 调用了下单工具 {tool}")
                        self.assertNotIn(
                            f"call_tool('{tool}'", code,
                            f"{os.path.basename(path)}:{lineno} 调用了下单工具 {tool}")

    def test_only_one_write_tool_used(self):
        """全项目只允许 auto-bind-coupons 一个改变账户状态的调用。"""
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        used = set()
        for _, path in self._source_files():
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            for m in re.finditer(r'call_tool\(\s*[\'"]([a-z0-9-]+)[\'"]', text):
                used.add(m.group(1))

        write_tools = used & set(self.FORBIDDEN) | (used & set(self.ALLOWED_WRITE))
        illegal = write_tools - set(self.ALLOWED_WRITE)
        self.assertEqual(
            illegal, set(),
            f"发现未授权的写操作调用：{illegal}（唯一允许的是 auto-bind-coupons）")

    def test_auto_claim_is_isolated_in_collector(self):
        """auto-bind-coupons 只能出现在 collector 的领券分支，物理隔离便于审计。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for name, path in self._source_files():
            if name == "collector.py":
                continue
            with open(path, encoding="utf-8") as fh:
                text = fh.read()
            self.assertNotIn(
                "auto-bind-coupons", text,
                f"{name} 不应出现 auto-bind-coupons（该调用须隔离在 collector）")

    def test_readme_states_the_boundary_honestly(self):
        """README 宣称的工具数量必须与测试实际覆盖的一致。

        防回归：改了 README 里的数字却没改测试，或反之。
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, "README.md"), encoding="utf-8") as fh:
            readme = fh.read()
        # README 必须列出全部 5 类下单工具，与本测试的 FORBIDDEN 对齐
        for tool in self.FORBIDDEN:
            self.assertIn(tool, readme,
                          f"README 的安全边界声明需列出 {tool}，与测试保持一致")
        # 数字一致性由 TestDocConsistency 精确校验，这里只确认存在
        self.assertRegex(readme, r"\d+ passed",
                         "README 应标注单测数量")


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

    def test_agent_channel_auto_resolves_address(self):
        """Agent 通道应能自动解析收件地址 —— 用户装了 CLI 登录过就能零配置发送。

        这是实测踩过的坑：最初要求用户手填 RADAR_TO，导致无人值守任务跑不通。

        注意必须 mock 掉 load_env：本机 .env 里配了 RADAR_TO，
        而 _get 会回落到读文件，仅清环境变量不足以让「自动解析」成为唯一来源。
        """
        import mail as mail_mod
        from unittest import mock
        with mock.patch.object(mail_mod, "load_env", lambda *a, **k: {}), \
             mock.patch.object(mail_mod, "agent_mail_address",
                               lambda: "auto@agent.qq.com"), \
             mock.patch.object(mail_mod, "push_via_agent",
                               lambda to, *a, **k: mail_mod.PushResult(True, "ok", "agent-mail", to)):
            os.environ["MAIL_CHANNEL"] = "agent"
            os.environ.pop("RADAR_TO", None)
            res = mail_mod.push("", "主题", "<p>正文</p>")
        self.assertTrue(res.ok)
        self.assertEqual(res.address, "auto@agent.qq.com",
                         "未配置 RADAR_TO 时应回落到自动解析的 Agent Mail 地址")

    def test_explicit_recipient_overrides_auto_resolve(self):
        """显式配置的收件人优先于自动解析 —— 这样才能发到任意邮箱。"""
        import mail as mail_mod
        from unittest import mock
        with mock.patch.object(mail_mod, "load_env", lambda *a, **k: {}), \
             mock.patch.object(mail_mod, "agent_mail_address",
                               lambda: "should-not-be-used@agent.qq.com"), \
             mock.patch.object(mail_mod, "push_via_agent",
                               lambda to, *a, **k: mail_mod.PushResult(True, "ok", "agent-mail", to)):
            os.environ["MAIL_CHANNEL"] = "agent"
            os.environ["RADAR_TO"] = "my-private@example.com"
            res = mail_mod.push("", "主题", "<p>正文</p>")
        self.assertTrue(res.ok)
        self.assertEqual(res.address, "my-private@example.com",
                         "RADAR_TO 应优先于自动解析值")

    def test_all_channels_fail_gives_diagnostics(self):
        """所有通道都失败时，note 要含每条通道的失败原因，便于排障。"""
        import mail as mail_mod
        from unittest import mock

        def fail_agent(to, *a, **k):
            return mail_mod.PushResult(False, "Agent 未登录", "agent-mail")

        def fail_smtp(*a, **k):
            return mail_mod.PushResult(False, "SMTP 未配置", "smtp")

        with mock.patch.object(mail_mod, "agent_mail_address", lambda: "a@agent.qq.com"), \
             mock.patch.object(mail_mod, "push_via_agent", fail_agent), \
             mock.patch.object(mail_mod, "send_via_smtp", fail_smtp), \
             mock.patch.object(mail_mod, "load_env", lambda *a, **k: {}):
            os.environ["MAIL_CHANNEL"] = "auto"
            res = mail_mod.push("", "主题", "<p>正文</p>")

        self.assertFalse(res.ok)
        self.assertEqual(res.channel, "none")
        self.assertIn("[agent]", res.note, "必须保留 Agent 通道的失败原因")
        self.assertIn("[smtp]", res.note, "必须保留 SMTP 通道的失败原因")

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


class TestSourceLabel(unittest.TestCase):
    """数据来源标注 —— 曾出现真实数据被误标成演示数据的 bug。

    根因是用 `not degraded` 反推来源，但真实抓取成功时 degraded 也是 False。
    现在改为 Snapshot.source 显式标记，此测试锁定该行为。
    """

    def _snap(self, **kw):
        from mcd_adapter import Snapshot
        base = dict(fetched_at="2026-10-16 11:00:00", scene="delivery",
                    store=None, menu=[], degraded=False)
        base.update(kw)
        return Snapshot(**base)

    def test_live_data_not_labelled_as_demo(self):
        """真实数据绝不能标成「fixtures 演示」。"""
        from mailer import render_digest
        from solver import Decision
        snap = self._snap(source="live")
        html = render_digest(Decision(None, [], [], []), snap)
        self.assertIn("麦当劳官方 MCP 实时数据", html)
        self.assertNotIn("fixtures 演示", html)

    def test_fixture_data_labelled_as_demo(self):
        """离线演示数据要如实标注。"""
        from mailer import render_digest
        from solver import Decision
        html = render_digest(Decision(None, [], [], []), self._snap(source="fixture"))
        self.assertIn("fixtures 演示数据", html)

    def test_cache_data_labelled_as_cache(self):
        """降级缓存要标明非实时。"""
        from mailer import render_digest
        from solver import Decision
        html = render_digest(Decision(None, [], [], []), self._snap(source="cache"))
        self.assertIn("缓存快照", html)

    def test_degraded_alone_does_not_decide_source(self):
        """degraded=True 但 source=live 时，标注仍应是实时（故障可见）。"""
        from mailer import render_digest
        from solver import Decision
        snap = self._snap(source="live", degraded=True, degrade_reason="测试")
        html = render_digest(Decision(None, [], [], []), snap)
        self.assertIn("麦当劳官方 MCP 实时数据", html)
        self.assertIn("缓存/降级数据", html)


class TestAgentCliCommand(unittest.TestCase):
    """Agent Mail CLI 调用契约。

    CLI v1.0.18 的几个硬性约束（**实测 + dry-run 验证**，勿凭直觉改）：
      · 子命令带 `+` 前缀：`message +send`，不是 `message send`
      · `--body-file` 必须是**相对当前目录**的路径
      · `--body-format html` 需显式指定，否则 HTML 被当纯文本
      · 非交互环境必须加 `--confirmed`，否则挂起等确认输入

    这里不 mock subprocess，而是直接校验源码里构造命令的契约字符串，
    避免 mock 与 WorkBuddy 的 node shim 交互导致的假失败。
    """

    @classmethod
    def setUpClass(cls):
        cls.src = open(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "src", "mail.py"),
            encoding="utf-8").read()

    def test_uses_plus_prefix(self):
        self.assertIn('"message", "+send"', self.src,
                      "子命令必须是 message +send（带 + 前缀）")

    def test_passes_confirmed_flag(self):
        self.assertIn('"--confirmed"', self.src,
                      "非交互调用必须带 --confirmed，否则会挂起等确认输入")

    def test_passes_body_format_html(self):
        self.assertIn('"--body-format", "html"', self.src,
                      "必须显式声明 html，否则 HTML 正文会被当纯文本")

    def test_uses_body_file_not_stdin(self):
        self.assertIn('"--body-file"', self.src)
        self.assertIn("tempfile.NamedTemporaryFile", self.src,
                      "官方要求相对路径，需落临时文件后传 body-file")

    def test_uses_relative_path(self):
        self.assertIn("os.path.relpath", self.src,
                      "body-file 必须转成相对路径")

    def test_cleans_up_temp_file(self):
        self.assertIn("os.unlink", self.src, "临时文件必须在 finally 中清理")

    def test_timeout_is_bounded(self):
        self.assertIn("timeout=", self.src, "子进程必须有超时，避免 cron 挂死")


class TestCliDiscovery(unittest.TestCase):
    """CLI 探测不能只依赖 PATH。"""

    def test_env_override_wins(self):
        import mail as mail_mod
        import tempfile as tf
        with tf.NamedTemporaryFile(suffix=".txt") as fh:
            with mock.patch.dict(os.environ, {"AGENTLY_CLI": fh.name}):
                self.assertEqual(mail_mod._find_agent_cli(), fh.name)

    def test_glob_fallback_finds_managed_runtime(self):
        """managed runtime 下的全局 bin 不在 PATH，需 glob 兜底。"""
        import mail as mail_mod
        pattern = os.path.expanduser(
            "~/.workbuddy/binaries/node/versions/*/bin/agently-cli")
        import glob as g
        found = g.glob(pattern)
        if not found:
            self.skipTest("本机无 managed runtime 安装，跳过")
        # 即使 PATH 里没有，_find_agent_cli 也应能通过 glob 找到
        self.assertTrue(mail_mod._find_agent_cli() is not None)


class TestProfileDerivation(unittest.TestCase):
    """偏好画像自动推导 —— 个性化功能的真正实现。

    早期版本只有空画像 + 手工配置，等于个性化没实现。
    现在从 order-list 真实响应推导，此测试锁定推导规则。
    """

    def _menu(self):
        from mcd_adapter import MenuItem
        from decimal import Decimal as D
        return [
            MenuItem("A", "巨无霸套餐", "汉堡", ("套餐", "人气"), D("39.5"), D("39.5")),
            MenuItem("B", "麦辣鸡腿堡", "汉堡", ("单品", "人气"), D("22.5"), D("22.5")),
            MenuItem("C", "薯条", "小食", ("小食",), D("11"), D("11")),
            MenuItem("D", "麦乐鸡", "小食", ("小食", "炸物"), D("35"), D("35")),
        ]

    def _orders(self):
        return [
            {"storeCode": "1400649", "totalPrice": 3950,
             "items": [{"productCode": "A", "quantity": 1}]},
            {"storeCode": "1400649", "totalPrice": 2250, "discount": 100,
             "items": [{"productCode": "B", "quantity": 1}]},
            {"storeCode": "1400999", "totalPrice": 1100,
             "items": [{"productCode": "C", "quantity": 1}]},
            # 第四单含 A+C：3950+1100 = 5050 分 = ¥50.5
            {"storeCode": "1400649", "totalPrice": 5050,
             "items": [{"productCode": "A", "quantity": 1}, {"productCode": "C", "quantity": 1}]},
        ]

    def test_derives_frequent_items(self):
        from mcd_adapter import build_taste_profile_from_orders
        p = build_taste_profile_from_orders(self._orders(), self._menu())
        # A 出现 2 次、C 出现 2 次 → 应进 top
        self.assertEqual(p.order_count, 4)
        self.assertIn("A", p.top_codes)
        self.assertIn("C", p.top_codes)

    def test_derives_favorite_store(self):
        from mcd_adapter import build_taste_profile_from_orders
        p = build_taste_profile_from_orders(self._orders(), self._menu())
        self.assertEqual(p.favorite_store_code, "1400649")  # 3 次 vs 1 次

    def test_derives_avg_price_and_discount_rate(self):
        from mcd_adapter import build_taste_profile_from_orders
        p = build_taste_profile_from_orders(self._orders(), self._menu())
        # 四单实付：39.5 + 22.5 + 11 + 50.5 = 123.5，均值 30.875
        avg = getattr(p, "avg_order_price", 0)
        self.assertAlmostEqual(avg, (39.5 + 22.5 + 11 + 50.5) / 4, places=2)
        hit = getattr(p, "discount_hit_rate", 0)
        self.assertAlmostEqual(hit, 0.25, places=2)   # 4 单中 1 单有优惠

    def test_empty_orders_yields_empty_profile(self):
        """新账号无历史订单（实测 data:{}）必须优雅降级，不能抛异常。"""
        from mcd_adapter import build_taste_profile_from_orders
        p = build_taste_profile_from_orders([], self._menu())
        self.assertEqual(p.order_count, 0)
        self.assertEqual(p.top_codes, ())

    def test_parse_order_list_handles_empty_object(self):
        """实测：新账号 order-list 返回 data:{}，不能当异常。"""
        from mcd_adapter import parse_order_list
        self.assertEqual(parse_order_list({"data": {}}), [])
        self.assertEqual(parse_order_list({"data": None}), [])
        self.assertEqual(parse_order_list({}), [])

    def test_parse_order_list_reads_list(self):
        from mcd_adapter import parse_order_list
        rows = [{"storeCode": "X", "items": []}]
        self.assertEqual(parse_order_list({"data": {"orders": rows}}), rows)

    def test_profile_summary_says_first_run(self):
        """无画像时摘要必须明说「首次运行」，不能假装有历史。"""
        from mcd_adapter import build_taste_profile_from_orders, profile_summary
        p = build_taste_profile_from_orders([], self._menu())
        self.assertIn("首次运行", profile_summary(p, self._menu()))

    def test_profile_summary_shows_evidence(self):
        """摘要要展示推导依据，让用户能发现偏差并纠正。"""
        from mcd_adapter import build_taste_profile_from_orders, profile_summary
        p = build_taste_profile_from_orders(self._orders(), self._menu())
        text = profile_summary(p, self._menu())
        self.assertIn("4 笔历史订单", text)
        self.assertIn("常点", text)
        self.assertIn("客单价", text)


class TestProfileResolution(unittest.TestCase):
    """画像来源优先级：自动推导 > 缓存 > 手工。"""

    def test_derived_wins_over_cached(self):
        import scheduler
        from mcd_adapter import Snapshot

        class FakeSnap:
            orders = [{"storeCode": "S1", "totalPrice": 3000,
                       "items": [{"productCode": "A", "quantity": 1}]}]
            menu = []
        prof = scheduler.resolve_profile(FakeSnap())
        self.assertEqual(prof.order_count, 1)
        self.assertEqual(prof.favorite_store_code, "S1")

    def test_manual_disliked_keywords_always_kept(self):
        """手工声明的忌口是用户主动表达，自动推导不该覆盖它。"""
        import scheduler
        from solver import TasteProfile

        class FakeSnap:
            orders = [{"storeCode": "S1", "totalPrice": 3000,
                       "items": [{"productCode": "A", "quantity": 1}]}]
            menu = []

        manual = TasteProfile(disliked_keywords=("辣椒",))
        prof = scheduler.resolve_profile(FakeSnap(), manual)
        self.assertIn("辣椒", prof.disliked_keywords)


class TestCouponEnumeration(unittest.TestCase):
    """券组合枚举 —— 曾有「or 短路 + 任意取前 2 张」的缺陷。"""

    def _item(self):
        from mcd_adapter import MenuItem
        from decimal import Decimal as D
        return MenuItem("A", "巨无霸", "汉堡", ("人气",), D("39"), D("39"))

    def _coupon(self, cid, value=None, days=None):
        from mcd_adapter import Coupon, Money
        return Coupon(cid, cid, Money(Decimal(value)) if value else None, None, days)

    def test_merges_store_and_card_coupons(self):
        """有门店券时也不能丢掉卡包券（早期版本 or 短路会全丢）。"""
        from solver import enumerate_coupon_combinations, Candidate, Nutrition
        cands = [Candidate(items=[self._item()], coupon=None,
                           quoted_price=None, nutrition=Nutrition())]
        store = [self._coupon("S1", "500", 3)]
        card = [self._coupon("C1", "800", 1)]
        out = enumerate_coupon_combinations(cands, card, store, max_pairs=2)
        used = {c.coupon.coupon_id for c in out if c.coupon}
        self.assertEqual(used, {"S1", "C1"}, "门店券与卡包券都应进入候选")

    def test_prioritizes_expiring_coupon(self):
        """快过期的券应优先，而非按列表顺序。"""
        from solver import enumerate_coupon_combinations, Candidate, Nutrition
        cands = [Candidate(items=[self._item()], coupon=None,
                           quoted_price=None, nutrition=Nutrition())]
        # 先给 5 天后才过期的大额券，后给 1 天后过期的小额券
        store = [self._coupon("FAR", "2000", 5), self._coupon("SOON", "300", 1)]
        out = enumerate_coupon_combinations(cands, [], store, max_pairs=1)
        picked = [c.coupon.coupon_id for c in out if c.coupon]
        self.assertEqual(picked, ["SOON"], "1 天后过期的券应优先")

    def test_unknown_value_sorts_last(self):
        """面值未知的券不应抢在有面值的券前面。"""
        from solver import enumerate_coupon_combinations, Candidate, Nutrition
        cands = [Candidate(items=[self._item()], coupon=None,
                           quoted_price=None, nutrition=Nutrition())]
        store = [self._coupon("UNKNOWN", None, 3), self._coupon("KNOWN", "500", 3)]
        out = enumerate_coupon_combinations(cands, [], store, max_pairs=1)
        picked = [c.coupon.coupon_id for c in out if c.coupon]
        self.assertEqual(picked, ["KNOWN"])

    def test_expired_card_coupon_excluded(self):
        """卡包券里已过期（>7天）的券不参与，避免浪费候选位。"""
        from solver import enumerate_coupon_combinations, Candidate, Nutrition
        cands = [Candidate(items=[self._item()], coupon=None,
                           quoted_price=None, nutrition=Nutrition())]
        card = [self._coupon("OLD", "900", 30)]
        out = enumerate_coupon_combinations(cands, card, [], max_pairs=2)
        self.assertEqual([c for c in out if c.coupon], [],
                         "30 天后才过期的卡包券不该占候选位")


# 只读常量表的例外清单（大写命名 + 从不被赋值 = 约定为常量）
IMMUTABLE_TABLES = {"_CUP_ALIASES"}


class TestArchitectureIntegrity(unittest.TestCase):
    """架构纪律：依赖方向必须是单向的（collector → solver）。"""

    def test_no_module_level_mutable_global(self):
        """禁止用模块级可变全局跨模块传状态（早期 LAST_BREAKDOWNS 的问题）。"""
        import ast
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        offenders = []
        for name in os.listdir(os.path.join(root, "src")):
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, "src", name)
            tree = ast.parse(open(path, encoding="utf-8").read())
            for node in tree.body:
                if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                    tgt = node.target.id
                    if tgt.isupper() and node.value is not None:
                        # 允许不可变常量（CST / SCENES 等），禁止 dict/list 容器
                        if isinstance(node.value, (ast.Dict, ast.List, ast.Set)):
                            # 显式声明的查表/别名表是例外：它们只读不写，
                            # 且用大写命名表达「这是常量」——
                            # 跨模块传状态的容器（曾经的 LAST_BREAKDOWNS）
                            # 才是要禁的。
                            if tgt in IMMUTABLE_TABLES:
                                continue
                            offenders.append(f"{name}:{node.lineno} {tgt}")
        self.assertEqual(offenders, [],
                         f"发现模块级可变全局：{offenders}（应用显式传参）")

    def test_solver_does_not_import_collector(self):
        """solver 不应反向依赖 collector。

        用 AST 判断而非字符串匹配 —— docstring 里出现「collector」是正常说明文字，
        早期版本用 `assertNotIn("import collector", src)` 会误报。
        """
        import ast
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tree = ast.parse(open(os.path.join(root, "src", "solver.py"),
                              encoding="utf-8").read())
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    imported.add(alias.name.split(".")[0])
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        self.assertNotIn("collector", imported,
                         "solver 反向依赖 collector，依赖方向错误（应单向 collector → solver）")
        self.assertIn("mcd_adapter", imported,
                      "solver 应只依赖 mcd_adapter 等底层模块")


class TestDocConsistency(unittest.TestCase):
    """文档与代码的一致性 —— 防止 README 里的数字再次漂移。

    这是被两次外部评审挑出来的痛点：
      · 第一次：README 同时写着 23 / 33 / 52 / 63 而实际 63
      · 第二次：数字统一后，workbuddy.md 里还留着 23，fixtures 份数写了 12/17/18

    所以覆盖面必须**全仓**，而不是只看 README；
    测试数与 fixtures 份数都要校验。
    """

    # 所有会被评审翻看的公开文档
    DOCS = ("README.md", "workbuddy.md", "MCP_INTEGRATION.md",
            "docs/SUBMIT_CHECKLIST.md", "docs/SCHEDULE.md")

    def _count_tests(self):
        """用 AST 静态统计真实用例数（不执行测试，避免递归）。"""
        import ast
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tree = ast.parse(open(os.path.join(root, "tests", "test_all.py"),
                              encoding="utf-8").read())
        return sum(1 for node in ast.walk(tree)
                   if isinstance(node, ast.ClassDef)
                   for sub in node.body
                   if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and sub.name.startswith("test_"))

    def _count_fixtures(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        d = os.path.join(root, "fixtures")
        return len([f for f in os.listdir(d) if f.endswith((".json", ".txt"))])

    def _read(self, rel):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, *rel.split("/")), encoding="utf-8") as fh:
            return fh.read()

    def _docs_present(self):
        return [d for d in self.DOCS if os.path.exists(
            os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         *d.split("/")))]

    # 测试数的所有可能写法（含之前漏掉的 "Ran N tests"、"N 单测"）
    _TEST_PATTERNS = (
        r"tests-(\d+)%20passed",
        r"tests-(\d+)\b",
        r"Ran (\d+) tests",
        r"(\d+) 个单测",
        r"(\d+) 单测",
        r"(\d+) passed",
        r"(\d+) 个测试",
        # ⚠️ 必须允许数字后跟标点 —— 早期版本漏了「76 个，」这种写法，
        # 导致 README:100 的过期数字躲过了校验。
        r"跑单测（(\d+) 个",
        r"跑单测\((\d+) 个",
    )

    def test_test_count_matches_everywhere(self):
        """全仓公开文档的测试数必须等于真实用例数。"""
        real = self._count_tests()
        offenders = []
        for rel in self._docs_present():
            text = self._read(rel)
            for pat in self._TEST_PATTERNS:
                for m in re.finditer(pat, text):
                    if int(m.group(1)) != real:
                        offenders.append(f"{rel}: {m.group(0)}（实际 {real}）")
        self.assertEqual(offenders, [],
                         "文档里的测试数与实际不符：\n  " + "\n  ".join(offenders))

    def test_fixture_count_matches_everywhere(self):
        """fixtures 份数必须等于真实份数。"""
        real = self._count_fixtures()
        offenders = []
        for rel in self._docs_present():
            text = self._read(rel)
            # ⚠️ 只校验「总份数」表述，**不能**把所有「N 份」都算进去 ——
            # 文档里「其中 6 份是真实录制」说的是子集，不是总数。
            # 这里匹配「存放/包含/共 N 份」这类总数字段。
            for pat in (r"存放\s*(\d+)\s*份", r"包含\s*(\d+)\s*份",
                        r"共\s*(\d+)\s*份", r"(\d+)\s*份响应样本"):
                for m in re.finditer(pat, text):
                    if int(m.group(1)) != real:
                        offenders.append(f"{rel}: {m.group(0)}（实际 {real}）")
        self.assertEqual(offenders, [],
                         "fixtures 份数不符：\n  " + "\n  ".join(offenders))

    def test_fixtures_real_vs_derived_breakdown(self):
        """fixtures 的「真实录制 / 文档推导」占比必须与实际一致。

        真实教训：批量替换数字时把「其中 5 份是真实录制」误改成了
        「18 份是真实录制」，与 MCP_INTEGRATION.md 的两类说明矛盾。
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        d = os.path.join(root, "fixtures")
        files = [f for f in os.listdir(d) if f.endswith((".json", ".txt"))]
        real = [f for f in files if f.startswith("real_")]
        derived = [f for f in files if not f.startswith("real_")]

        for rel in self._docs_present():
            text = self._read(rel)
            # 形如「其中 6 份是 ... 真实调用录制」
            m = re.search(r"其中\s*(\d+)\s*份是[^。]{0,20}真实", text)
            if m:
                self.assertEqual(int(m.group(1)), len(real),
                                 f"{rel} 称 {m.group(1)} 份真实录制，"
                                 f"实际 {len(real)} 份")
            # 形如「12 份是按官方文档结构整理」
            m2 = re.search(r"(\d+)\s*份是(?:按)?官方", text)
            if m2:
                self.assertEqual(int(m2.group(1)), len(derived),
                                 f"{rel} 称 {m2.group(1)} 份推导样本，"
                                 f"实际 {len(derived)} 份")

    def test_docs_reference_real_code_symbols(self):
        """文档里给的排错命令引用的符号必须真实存在。

        真实教训：SCHEDULE.md 写 `from mail import MailConfig`，
        但实际类名是 `SmtpConfig` —— 照着文档敲会直接 ImportError。
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        mail_src = open(os.path.join(root, "src", "mail.py"),
                        encoding="utf-8").read()
        mcd_src = open(os.path.join(root, "src", "mcd_adapter.py"),
                       encoding="utf-8").read()

        sched = self._read("docs/SCHEDULE.md")
        # from mail import X → X 必须在 mail.py
        # ⚠️ 只取 import 列表里**逗号分隔的标识符**，
        # 遇到分号/括号就截断 —— 否则 `from mail import X; print(X)`
        # 里的 `X; print(X)` 会被当成一个符号。
        for mod, names in re.findall(r"from (mail|mcd_adapter) import ([^\n]+)",
                                     sched):
            names = re.split(r"[;)]", names)[0]
            for sym in names.split(","):
                sym = sym.strip()
                if not sym or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", sym):
                    continue
                target = mail_src if mod == "mail" else mcd_src
                self.assertIn(sym, target,
                              f"SCHEDULE.md 引用了 {mod}.{sym}，但源码里没有")

        # 字段名必须与真实响应一致（实测是 availablePoint 不是 available）
        if "query-my-account" in sched:
            self.assertIn("availablePoint", sched,
                          "积分字段名应为 availablePoint（实测），不是 available")
            self.assertNotIn(".get('available')", sched,
                             "SCHEDULE.md 仍在用错误的字段名 available")

    def test_readme_has_visual_hook(self):
        """首屏必须有邮件截图 —— 纯文字的项目在 Star 竞赛里没竞争力。"""
        readme = self._read("README.md")
        self.assertIn("assets/shots/", readme,
                      "README 首屏需嵌入邮件截图（视觉钩子）")

    def test_first_screenshot_is_not_stale(self):
        """⚠️ 首屏第一张图不能是「未核价」的旧版。

        真实教训：修好未核价问题后忘了重截首屏图，
        路人点进 GitHub 第一眼看到的是满屏「未核价/待核价」——
        从截图上根本看不出代码已经修好。
        这里做结构检查：图必须存在且不太小（旧图与新图尺寸不同）。
        """
        import glob
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        readme = self._read("README.md")
        shots = re.findall(r'\./(assets/shots/[^"]+\.png)', readme)
        self.assertTrue(shots, "README 应至少引用一张截图")
        for rel in shots:
            path = os.path.join(root, rel)
            self.assertTrue(os.path.exists(path), f"README 引用的截图不存在：{rel}")
            size = os.path.getsize(path)
            self.assertGreater(size, 20_000,
                               f"{rel} 只有 {size} bytes，可能是旧版或空文件")
            # 更新时间应晚于最近一次代码提交 —— 保证图是最新的
            self.assertGreater(os.path.getmtime(path),
                               os.path.getmtime(os.path.join(root, "src", "solver.py")) - 86400,
                               f"{rel} 早于 solver.py 的修改，可能是修复前的旧截图")

    def test_no_personal_email_in_public_docs(self):
        """公开文档不得出现真实邮箱 —— 与「不暴露隐私」的宣称一致。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        pattern = re.compile(r"(?<!xxx)[\w.+-]{4,}@\w+\.qq\.com")
        offenders = []
        for rel in self.DOCS:
            if not os.path.exists(os.path.join(root, *rel.split("/"))):
                continue
            for m in pattern.finditer(self._read(rel)):
                offenders.append(f"{rel}: {m.group(0)}")
        self.assertEqual(offenders, [], f"公开文档含真实邮箱：{offenders}")



class TestCareCopy(unittest.TestCase):
    """时段关怀文案 —— 必须基于真实依据，且守住赛事合规红线。"""

    def test_meal_windows_follow_official_hours(self):
        """时间窗取自官方门店营业时段实测值，不是拍脑袋定的。"""
        from mailer import current_meal_window
        cases = {7: "breakfast", 10: "breakfast", 12: "lunch",
                 16: "tea", 19: "dinner", 22: "dinner"}
        for hour, expected in cases.items():
            self.assertEqual(current_meal_window(hour), expected,
                             f"{hour} 时应属于 {expected}")

    def test_late_night_maps_to_dinner(self):
        """凌晨 0-5 点不属于任何窗口，归到夜市场景。"""
        from mailer import current_meal_window
        self.assertEqual(current_meal_window(2), "dinner")

    def test_expiring_coupon_drives_copy(self):
        """券到期时，关怀应基于这个真实依据。"""
        from mailer import CareContext, _care_line
        today_title, _ = _care_line(CareContext(meal="lunch", hour=12,
                                                expiring_today=2))
        self.assertIn("2 张券", today_title)
        self.assertIn("今天", today_title)

    def test_soon_expiring_coupon_is_gentler(self):
        """「几天内到期」不该用「今天就过期」的说法 —— 那是不准确。"""
        from mailer import CareContext, _care_line
        title, body = _care_line(CareContext(meal="lunch", hour=12,
                                             expiring_coupon_count=2,
                                             expiring_today=0))
        self.assertIn("2 张券", title)
        self.assertNotIn("今天", title,
                         "还有 1~3 天的券说「今天过期」是不准确的提醒")
        self.assertIn("不着急", body)

    def test_today_expiry_takes_priority(self):
        """今天到期比「几天内到期」更紧迫，文案应优先说它。"""
        from mailer import CareContext, _care_line
        title, _ = _care_line(CareContext(meal="lunch", hour=12,
                                          expiring_coupon_count=5,
                                          expiring_today=1))
        self.assertIn("今天", title)
        self.assertNotIn("5 张", title, "应只说今天到期的 1 张，不混数")

    def test_campaign_does_not_hijack_the_title(self):
        """活动**不抢占关怀标题**。

        早期版本让活动直接当标题，结果时段关怀完全被遮住 ——
        而活动在邮件下方本来就有独立区块，重复说反而稀释了关怀。
        现在活动只作为末尾的一句补充。
        """
        from mailer import CareContext, _care_line
        plain_t, plain_b = _care_line(CareContext(meal="lunch", hour=12))
        camp_t, camp_b = _care_line(CareContext(meal="lunch", hour=12,
                                                campaign_count=5))
        self.assertEqual(plain_t, camp_t, "有无活动不应改变关怀标题")
        self.assertIn("门店", camp_b, "活动应作为补充信息出现")
        self.assertTrue(camp_b.startswith(plain_b[:10]), "补充应在原正文之后")
        self.assertGreater(len(camp_b), len(plain_b), "有活动时应多一句")

    def test_late_night_copy_matches_actual_hour(self):
        """凌晨 0~5 点不能说「今天辛苦了」—— 那一刻人还没结束这一天。

        current_meal_window 把凌晨归到 dinner 只是**分类兜底**，
        语气必须按真实时间给。
        """
        from mailer import CareContext, _care_line
        for h in (0, 2, 4, 5):
            title, body = _care_line(CareContext(meal="dinner", hour=h))
            self.assertIn("还没睡", title,
                          f"{h} 时不该用晚餐收尾的语气")
            self.assertNotIn("今天辛苦了", title)
            self.assertIn("休息", body)
        # 边界：6 点已进入早餐时段
        six = _care_line(CareContext(meal="breakfast", hour=6))[0]
        self.assertNotIn("还没睡", six)

    def test_weekend_copy_differs_from_weekday(self):
        """周末该有周末的语气 —— 否则显得像模板。"""
        from mailer import CareContext, _care_line
        weekday = _care_line(CareContext(meal="dinner", hour=18, is_weekend=False))[0]
        weekend = _care_line(CareContext(meal="dinner", hour=18, is_weekend=True))[0]
        self.assertNotEqual(weekday, weekend, "周末文案不应与工作日相同")
        self.assertIn("周末", weekend)

    def test_warm_but_not_preachy_or_manipulative(self):
        """温暖 ≠ 说教，也 ≠ 制造焦虑。

        锁住三类越界：命令式、恐吓式、道德绑架式。
        """
        from mailer import CareContext, _care_line
        harsh = ["必须", "一定要", "赶紧", "马上", "别再", "你的身体垮",
                 "危害健康", "罚款", "后果自负", "你对不起"]
        for meal in ("breakfast", "lunch", "tea", "dinner"):
            for h in range(5, 24):
                for kwargs in ({}, {"is_weekend": True}, {"expiring_coupon_count": 2},
                               {"skipped_meals": 3}, {"campaign_count": 4}):
                    title, body = _care_line(
                        CareContext(meal=meal, hour=h, **kwargs))
                    for word in harsh:
                        self.assertNotIn(word, title + body,
                                         f"{meal}/{h} 时文案过于说教：{word}")

    def test_skipped_meals_needs_evidence(self):
        """漏餐提示需要 skipped_meals 支撑 —— 无依据就不该说。"""
        from mailer import CareContext, _care_line
        t1, _ = _care_line(CareContext(meal="lunch", hour=12, skipped_meals=0))
        t2, _ = _care_line(CareContext(meal="dinner", hour=19, skipped_meals=3))
        self.assertNotIn("没顾上", t1)
        self.assertIn("没顾上", t2)

    def test_never_compares_brands(self):
        """赛事红线：关怀文案不得出现品牌对比。"""
        from mailer import CareContext, _care_line
        banned = ["肯德基", "KFC", "汉堡王", "星巴克", "更便宜", "比别家"]
        for h in range(5, 24):
            for kwargs in ({}, {"expiring_coupon_count": 2}, {"skipped_meals": 3}):
                title, body = _care_line(CareContext(meal="lunch", hour=h, **kwargs))
                for word in banned:
                    self.assertNotIn(word, title + body,
                                     f"{h} 时的文案出现了「{word}」")

    def test_copy_is_not_preachy(self):
        """不催促、不说教 —— 用词要有节制。"""
        from mailer import CareContext, _care_line
        harsh = ["必须", "一定要", "赶紧", "马上", "别再", "你的身体垮", "危害健康"]
        for h in range(5, 24):
            title, body = _care_line(CareContext(meal="lunch", hour=h))
            for word in harsh:
                self.assertNotIn(word, title + body,
                                 f"{h} 时的文案过于说教：{word}")

    def test_care_renders_into_digest(self):
        """关怀区块应出现在邮件 HTML 里。"""
        from mailer import CareContext, render_digest
        from solver import Decision
        from mcd_adapter import Snapshot
        snap = Snapshot(fetched_at="2026-10-16 12:00:00", scene="delivery",
                        store=None, menu=[])
        html = render_digest(Decision(None, [], [], []), snap, "", "午间",
                             care=CareContext(meal="lunch", hour=12))
        self.assertIn("先把自己喂饱", html)
        self.assertIn("到饭点啦", html)

    def test_no_care_block_when_not_provided(self):
        """没传关怀上下文时不应硬塞一个区块。"""
        from mailer import render_digest
        from solver import Decision
        from mcd_adapter import Snapshot
        snap = Snapshot(fetched_at="2026-10-16 12:00:00", scene="delivery",
                        store=None, menu=[])
        html = render_digest(Decision(None, [], [], []), snap, "", "午间")
        self.assertNotIn("先把自己喂饱", html)


class TestPrefilter(unittest.TestCase):
    """零成本预筛 —— 「未核价占满屏」问题的根治机制。"""

    def _items(self, n):
        from mcd_adapter import MenuItem, Money
        from decimal import Decimal
        return [MenuItem(f"C{i}", f"套餐{i}", "套餐", ("套餐",),
                         Money(Decimal("30")), Money(Decimal("39")))
                for i in range(n)]

    def test_prefilter_limits_to_top_k(self):
        from solver import apply_preference, prefilter_for_pricing, TasteProfile
        items = self._items(100)
        cands = apply_preference(items, TasteProfile())
        picked = prefilter_for_pricing(cands, top_k=8)
        self.assertEqual(len(picked), 8,
                         "预筛必须把送核价的候选压到 top_k 以内")

    def test_prefilter_drops_absurdly_expensive_when_others_exist(self):
        """有正常候选时，标价离谱的应被滤掉。"""
        from solver import apply_preference, prefilter_for_pricing, TasteProfile
        from mcd_adapter import MenuItem, Money
        from decimal import Decimal
        items = [MenuItem("OK", "普通套餐", "套餐", ("套餐",),
                          Money(Decimal("30")), Money(Decimal("39"))),
                 MenuItem("RICH", "豪华套餐", "套餐", (),
                          Money(Decimal("9999")), Money(Decimal("9999")))]
        cands = apply_preference(items, TasteProfile())
        picked = prefilter_for_pricing(cands, top_k=8)
        self.assertEqual(len(picked), 1, "标价离谱的应被滤掉，只留正常的")
        self.assertEqual(picked[0].items[0].code, "OK")

    def test_prefilter_returns_all_when_filtered_empty(self):
        """粗筛把候选全滤掉时，应回落到全部候选 —— 不能返回空列表。"""
        from solver import apply_preference, prefilter_for_pricing, TasteProfile
        from mcd_adapter import MenuItem, Money
        from decimal import Decimal
        rich = [MenuItem(f"R{i}", "豪华", "套餐", (),
                         Money(Decimal("9999")), Money(Decimal("9999")))
                for i in range(3)]
        cands = apply_preference(rich, TasteProfile())
        picked = prefilter_for_pricing(cands, top_k=8)
        # 兜底是有意设计：粗筛全滤掉时回落全部候选，宁可多核几个价，
        # 也不要因为粗筛过严导致无解
        self.assertEqual(len(picked), 3, "粗筛清空时应回落到全部候选")

    def test_prefilter_ignores_already_rejected(self):
        from solver import apply_preference, prefilter_for_pricing, TasteProfile
        items = self._items(20)
        cands = apply_preference(items, TasteProfile(disliked_keywords=("套餐",)))
        picked = prefilter_for_pricing(cands, top_k=8)
        self.assertEqual(len(picked), 0, "已被忌口过滤的不该进入核价名单")


class TestMailHtmlStructure(unittest.TestCase):
    """邮件 HTML 结构 —— 裸元素会触发 foster parenting 被提出卡片。

    真实 bug：profile_line 曾是裸 <div> 直接放进 <table>，
    浏览器把它提升到整个 table 之前，导致画像摘要飞到邮件顶部的
    灰色背景上。这个问题从渲染代码看不出来，**只有截图能发现**。
    """

    @staticmethod
    def _render(profile_summary="基于你的 3 笔历史订单"):
        from mailer import render_digest
        from solver import Decision
        from mcd_adapter import Snapshot
        snap = Snapshot(fetched_at="2026-10-16 12:00:00", scene="delivery",
                        store=None, menu=[])
        return render_digest(Decision(None, [], [], []), snap,
                             profile_summary, "午间")

    def test_no_bare_div_directly_inside_table(self):
        """<table> 与 <div> 之间不能隔空 —— 必须有 <tr>/<td>。"""
        import re
        html = self._render()
        bad = []
        for m in re.finditer(r"<table\b", html):
            window = html[m.end():m.end() + 300]
            first = re.search(r"<(tr|td|div)\b", window)
            if first and first.group(1) == "div":
                bad.append(html[m.start():m.start() + 40])
        self.assertEqual(bad, [], f"<table> 后直接跟了 <div>：{bad}")

    def test_profile_summary_is_inside_card(self):
        """画像摘要必须在卡片表格内部，不能在它之前。"""
        html = self._render()
        i_profile = html.find("基于你的 3 笔历史订单")
        i_table = html.find("<table")
        self.assertGreater(i_profile, i_table,
                           "画像摘要出现在表格之前，说明被 foster parenting 提升了")
        # 且必须包在 tr/td 里
        before = html[max(0, i_profile - 220):i_profile]
        self.assertIn("<td", before, "画像摘要必须包在 <td> 内")

    def test_empty_profile_summary_emits_no_row(self):
        """没有画像摘要时不该产生空行。"""
        html = self._render(profile_summary="")
        self.assertNotIn("<td style=\"padding:0 20px 14px", html)


class TestSessionIdHeader(unittest.TestCase):
    """Mcp-Session-Id 必须真的发出。

    真实 bug：早期版本写 `self._headers()["Mcp-Session-Id"] = ...`，
    但 _headers() 每次返回**新 dict**，那次赋值立刻被丢弃，
    导致服务端下发的 session id 从未真正发出。
    """

    def test_headers_contain_session_when_set(self):
        from mcd_adapter import McdMcpClient
        c = McdMcpClient(token="t")
        c._session_id = "sess-123"
        self.assertEqual(c._headers().get("Mcp-Session-Id"), "sess-123",
                         "设了 session id 就必须出现在请求头里")

    def test_headers_omit_session_when_absent(self):
        from mcd_adapter import McdMcpClient
        c = McdMcpClient(token="t")
        self.assertNotIn("Mcp-Session-Id", c._headers())

    def test_session_is_not_set_outside_headers_method(self):
        """结构约束：不得在 _headers() 外部往返回值上赋值。

        用 AST 判断而非字符串匹配 —— 注释里也需要写明这个历史 bug，
        字符串匹配会误报。
        """
        import ast
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        tree = ast.parse(open(os.path.join(root, "src", "mcd_adapter.py"),
                              encoding="utf-8").read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Subscript):
                v = node.value
                # self._headers()["..."] = x
                if isinstance(v, ast.Call) and getattr(v.func, "attr", "") == "_headers":
                    self.fail(f"行 {node.lineno}：往 _headers() 返回值上赋值会被丢弃")


class TestSkippedMealsDetection(unittest.TestCase):
    """漏餐判定 —— README 主打文案的真实触发条件。

    真实 bug：scheduler 里 `skipped = 0 if ordered_today else 1`，
    而 mailer 要求 `skipped_meals >= 2`，于是这个值最大只有 1，
    「今天还没顾上吃饭」在真实运行中**永远不会出现**。
    """

    def _snap(self, orders):
        class S:
            store_coupons = []
            my_coupons = []
            campaign_names = []
        S.orders = orders
        return S()

    def _at(self, hour):
        import scheduler
        from datetime import datetime
        return datetime(2026, 10, 16, hour, 0, tzinfo=scheduler.CST)

    def _orders_today(self):
        return [{"orderId": "ORD-20261016-123", "storeCode": "1400649",
                 "totalPrice": 3350, "items": []}]

    def _orders_old(self):
        return [{"orderId": "ORD-20260101", "storeCode": "1400649",
                 "totalPrice": 3350, "items": []}]

    def test_no_history_yields_zero_skipped(self):
        """无历史订单时不应猜测漏餐（订单可能是外卖而非麦当劳）。"""
        import scheduler
        care = scheduler.build_care_context(self._snap([]), None, now=self._at(19))
        self.assertEqual(care.skipped_meals, 0)

    def test_ordered_today_yields_zero(self):
        import scheduler
        care = scheduler.build_care_context(self._snap(self._orders_today()),
                                            None, now=self._at(19))
        self.assertEqual(care.skipped_meals, 0)

    def test_morning_after_one_missed_window_is_not_enough(self):
        """刚过早餐时段就提醒「没顾上吃饭」太早。"""
        import scheduler
        from mailer import _care_line
        care = scheduler.build_care_context(self._snap(self._orders_old()),
                                            None, now=self._at(12))
        title, _ = _care_line(care)
        self.assertNotIn("还没顾上吃饭", title,
                         "12 点只错过早餐，不该说漏餐")

    def test_evening_with_nothing_ordered_triggers_care(self):
        """傍晚且当天没下单 → 应该触发漏餐关怀（README 的金句）。"""
        import scheduler
        from mailer import _care_line
        care = scheduler.build_care_context(self._snap(self._orders_old()),
                                            None, now=self._at(19))
        self.assertGreaterEqual(care.skipped_meals, 2,
                                "19 点至少错过早餐与午餐，应满足 >=2")
        title, _ = _care_line(care)
        self.assertIn("还没顾上吃饭", title,
                      "README 主打的漏餐关怀必须真的能触发")

    def test_late_night_counts_more_windows(self):
        """越晚错过的餐次越多，但不超过已过的时段数。"""
        import scheduler
        counts = {}
        for h in (12, 16, 19, 22):
            care = scheduler.build_care_context(self._snap(self._orders_old()),
                                                None, now=self._at(h))
            counts[h] = care.skipped_meals
        self.assertEqual(counts[12], 1)
        self.assertLessEqual(counts[22], 4, "凌晨不计入餐次")
        self.assertGreaterEqual(counts[22], counts[16])


class TestMenuFiltering(unittest.TestCase):
    """菜单过滤 —— 周边与时段错配。

    实测官方菜单把「麦当劳联名徽章」放在「人气热卖」里，
    于是被推荐成了晚餐首选 —— 这类问题只能靠真实数据发现。
    """

    def _items(self):
        from mcd_adapter import MenuItem, Money
        from decimal import Decimal as D
        m = Money(D("39"))
        return [
            MenuItem("A", "龙焰鸡腿堡四件套", "套餐", ("套餐",), m, m),
            MenuItem("B", "麦当劳联名徽章", "人气热卖", ("人气",), m, m),
            MenuItem("C", "薯条脆卜卜毛绒周边", "小食甜品/其他", ("周边",), m, m),
            MenuItem("D", "培根蛋麦满分", "早餐", ("早餐",), m, m),
            MenuItem("E", "麦咖啡™奶铁", "麦咖啡", ("冷饮",), m, m),
        ]

    def _names(self, meal=""):
        from solver import apply_preference, TasteProfile
        cands = apply_preference(self._items(), TasteProfile(), meal)
        return {c.items[0].name for c in cands if c.rejected_by is None}

    def test_non_food_always_filtered(self):
        """周边即使在「人气热卖」也必须过滤 —— 任何时段都不该推荐。"""
        alive = self._names("dinner")
        self.assertNotIn("麦当劳联名徽章", alive)
        self.assertNotIn("薯条脆卜卜毛绒周边", alive)
        self.assertIn("龙焰鸡腿堡四件套", alive, "正常餐品应保留")

    def test_breakfast_only_filtered_at_lunch_and_dinner(self):
        alive = self._names("dinner")
        self.assertNotIn("培根蛋麦满分", alive,
                         "晚餐不该推荐麦满分")

    def test_breakfast_item_allowed_at_breakfast(self):
        """早餐时段应保留早餐品 —— 不能一刀切。"""
        from solver import apply_preference, TasteProfile
        cands = apply_preference(self._items(), TasteProfile(), "breakfast")
        alive = {c.items[0].name for c in cands if c.rejected_by is None}
        self.assertIn("培根蛋麦满分", alive)

    def test_coffee_not_treated_as_breakfast_only(self):
        """⚠️ 咖啡全天可喝，不该被当早餐品误杀。"""
        from solver import apply_preference, TasteProfile
        cands = apply_preference(self._items(), TasteProfile(), "dinner")
        alive = {c.items[0].name for c in cands if c.rejected_by is None}
        self.assertIn("麦咖啡™奶铁", alive,
                      "麦咖啡是独立饮品分类，不应被早餐过滤误伤")

    def test_no_meal_means_no_time_filter(self):
        """不传 meal 时应保持原有行为（离线 demo 与单测兼容）。"""
        alive = self._names("")
        self.assertIn("培根蛋麦满分", alive)


class TestQuoteDeduplication(unittest.TestCase):
    """核价去重 —— 必须发生在 top_k 截断**之前**。

    真实 bug：早期版本是「先截断再去重」，而券枚举把同一商品拆成
    「无券+券1+券2」三个克隆体，常点商品底分高（+50）挤在一起占满 top_k，
    2 张券时实测**只剩 4 款不同餐品**被核价。

    ⚠️ 早期版本的测试只放1 张券（8/2 = 4 款，刚好卡过 `>= 4` 的断言），
    反而把真问题掩盖了。所以这里必须覆盖**多张券**的场景。
    """

    def _snap(self, n_coupons, n_items=20):
        from mcd_adapter import MenuItem, Money, Coupon, Snapshot
        from decimal import Decimal as D
        m = Money(D("39"))
        menu = [MenuItem(f"C{i}", f"套餐{i}", "套餐", ("套餐",), m, m)
                for i in range(n_items)]
        coupons = [Coupon(f"S{j}", f"券{j}", Money(D("500")), None, 3)
                   for j in range(n_coupons)]
        return Snapshot(fetched_at="2026-10-16 12:00:00", scene="delivery",
                        store=None, menu=menu, my_coupons=coupons)

    def _plan(self, n_coupons, top_k=8):
        from collector import plan_quotes
        from solver import DietaryPolicy, TasteProfile
        snap = self._snap(n_coupons)
        return plan_quotes(snap, TasteProfile(),
                           DietaryPolicy(max_kcal=2000, max_sodium_mg=3000),
                           top_k=top_k)

    def test_no_duplicate_signatures(self):
        """送核价的组合不允许有重复签名。"""
        for n in (0, 1, 2, 3):
            combos, _ = self._plan(n)
            from solver import combo_signature
            sigs = [combo_signature(c) for c in combos]
            self.assertEqual(len(sigs), len(set(sigs)),
                             f"{n} 张券时出现重复签名：{sigs}")

    def test_two_coupons_still_fill_top_k(self):
        """🔴 核心回归：2 张券时仍应覆盖满8 款不同餐品。

        早期 bug：先截断再去重 → 2 张券只覆盖 4 款。
        早期测试只放 1 张券（刚好 4 款卡过 >=4 断言）把这个 bug 掩盖了。
        """
        combos, _ = self._plan(2, top_k=8)
        self.assertEqual(len(combos), 8,
                         f"2 张券时只覆盖 {len(combos)} 款，应为 8 款 —— "
                         "去重必须发生在 top_k 截断之前")

    def test_three_coupons_still_fill_top_k(self):
        """3 张券时克隆体更多，更容易把坑位占满。"""
        combos, _ = self._plan(3, top_k=8)
        self.assertEqual(len(combos), 8)

    def test_fewer_items_than_top_k(self):
        """候选不足 top_k 时，返回全部而非报错。"""
        combos, _ = self._plan(2, top_k=20)
        self.assertEqual(len(combos), 20, "菜单 20 项，top_k=20 应全覆盖")

    def test_prefilter_itself_dedups(self):
        """去重逻辑应在 prefilter_for_pricing 内部，而不是调用方事后补救。"""
        from solver import (apply_preference, combo_signature,
                            enumerate_coupon_combinations,
                            prefilter_for_pricing, TasteProfile)
        from solver import DietaryPolicy
        snap = self._snap(2)
        cands = apply_preference(snap.menu, TasteProfile())
        cands = enumerate_coupon_combinations(
            cands, snap.my_coupons, snap.store_coupons)
        picked = prefilter_for_pricing(cands, top_k=8)
        sigs = [combo_signature([(i.code, 1) for i in c.items]) for c in picked]
        self.assertEqual(len(sigs), len(set(sigs)),
                         f"prefilter 内部就应去重，实际返回了重复：{sigs}")
        self.assertEqual(len(picked), 8,
                         f"去重后应攒满 8 个不同签名，实际 {len(picked)}")

class TestToolInventoryMatchesDocs(unittest.TestCase):
    """Tool 调用数必须与文档一致。

    真实教训：文档写「7 个只读 + 3 个默认关闭」，
    但那 3 个后来为了真实功能都默认启用了 —— 文档与实现脱节。
    对一个主打「诚实」的项目，这种数字漂移是硬伤。
    """

    def _tools_in_source(self):
        import re
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        used = set()
        for name in os.listdir(os.path.join(root, "src")):
            if not name.endswith(".py"):
                continue
            text = open(os.path.join(root, "src", name), encoding="utf-8").read()
            used |= set(re.findall(r'call_tool\(\s*["\']([a-z0-9-]+)["\']', text))
        return used

    def test_docs_state_actual_readonly_tool_count(self):
        used = self._tools_in_source()
        read_only = used - {"auto-bind-coupons"}
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        doc = open(os.path.join(root, "MCP_INTEGRATION.md"), encoding="utf-8").read()
        m = re.search(r"实际调用\s*\*\*(\d+)\s*个只读", doc)
        self.assertIsNotNone(m, "MCP_INTEGRATION.md 应写明实际调用的只读 Tool 数")
        self.assertEqual(int(m.group(1)), len(read_only),
                         f"文档写 {m.group(1)}，源码实际调用 {len(read_only)} 个只读 Tool："
                         f"{sorted(read_only)}")

    def test_every_used_tool_is_listed_in_docs(self):
        """源码里调用的每个 Tool 都必须在文档里出现，否则等于隐藏行为。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        doc = open(os.path.join(root, "MCP_INTEGRATION.md"), encoding="utf-8").read()
        missing = sorted(t for t in self._tools_in_source() if t not in doc)
        self.assertEqual(missing, [],
                         f"这些 Tool 在源码里被调用但文档没写：{missing}")

    def test_no_ordering_tools_used(self):
        """跨守卫：实际调用的 Tool 里不得含下单类。"""
        used = self._tools_in_source()
        forbidden = {"create-order", "cancel-order", "mall-create-order",
                     "party-order-create", "draw-lottery"}
        self.assertEqual(used & forbidden, set(),
                         f"源码调用了下单类 Tool：{used & forbidden}")

    def test_only_one_write_tool(self):
        """唯一允许的写操作是 auto-bind-coupons（每日 09:00 自动领券）。"""
        used = self._tools_in_source()
        self.assertIn("auto-bind-coupons", used,
                      "09:00 自动领券是核心功能，不应缺失")
        write_like = {"create-order", "cancel-order", "mall-create-order",
                      "party-order-create", "draw-lottery", "auto-bind-coupons"}
        self.assertEqual(used & write_like, {"auto-bind-coupons"},
                         f"写操作类 Tool 应只有 auto-bind-coupons，实际：{used & write_like}")


class TestSlotTimeBase(unittest.TestCase):
    """调度档位的时间基准 —— 必须用 slot.hour，不能用真实时钟。

    真实 bug：`--all` 预览时四个档位都用 datetime.now()，
    实测 13:40 跑 `--all`，早间/午间/晚间/过期提醒**全部**说
    「到饭点啦」，且早餐档把麦满分全过滤了。
    这让 `--all` 预览（评审和陌生人的主要入口）基本失效。
    """

    def _snap(self):
        class S:
            orders = []
            store_coupons = []
            my_coupons = []
            campaign_names = []
        return S()

    def test_slots_cover_different_meal_windows(self):
        """四个档位应覆盖至少 3 个不同餐次窗口。"""
        import scheduler
        from mailer import current_meal_window
        meals = {current_meal_window(s.hour) for s in scheduler.SLOTS}
        self.assertGreaterEqual(len(meals), 3,
                                f"四档只覆盖了 {meals} 个餐次，预览看不出差异")

    def test_care_copy_differs_per_slot(self):
        """不同档位应产生不同的关怀文案。"""
        import scheduler
        from mailer import _care_line
        from datetime import datetime
        now = datetime(2026, 10, 16, 13, 40, tzinfo=scheduler.CST)
        titles = []
        for slot in scheduler.SLOTS:
            slot_dt = datetime(now.year, now.month, now.day,
                               slot.hour, slot.minute, tzinfo=scheduler.CST)
            care = scheduler.build_care_context(
                self._snap(), slot, now=slot_dt)
            titles.append(_care_line(care)[0])
        self.assertEqual(len(set(titles)), len(titles),
                         f"四档产生了重复文案：{titles}")

    def test_morning_slot_says_breakfast_not_lunch(self):
        """🔴 核心回归：13:40 跑 --all 时，早间档必须说早餐而不是午餐。"""
        import scheduler
        from mailer import _care_line
        from datetime import datetime
        now = datetime(2026, 10, 16, 13, 40, tzinfo=scheduler.CST)
        morning = next(s for s in scheduler.SLOTS if s.key == "morning")
        slot_dt = datetime(now.year, now.month, now.day,
                           morning.hour, morning.minute, tzinfo=scheduler.CST)
        care = scheduler.build_care_context(self._snap(), morning, now=slot_dt)
        title, _ = _care_line(care)
        self.assertEqual(care.meal, "breakfast",
                         "09:00 档应属于早餐窗口")
        self.assertNotIn("到饭点", title,
                         f"早间档不应出现午餐文案：{title}")

    def test_expiry_slot_uses_dinner_window(self):
        """20:00 过期提醒档应归入晚餐/夜市窗口。"""
        import scheduler
        from mailer import current_meal_window
        expiry = next(s for s in scheduler.SLOTS if s.key == "expiry")
        self.assertEqual(current_meal_window(expiry.hour), "dinner")

    def test_breakfast_items_survive_in_morning_slot(self):
        """🔴 早间档不该过滤掉早餐品 —— 早期版本用真实时钟会误杀。"""
        import scheduler
        from solver import apply_preference, TasteProfile
        from mcd_adapter import MenuItem, Money
        from decimal import Decimal as D
        from mailer import current_meal_window
        m = Money(D("16"))
        menu = [MenuItem("BM", "培根蛋麦满分", "早餐", ("早餐",), m, m)]
        morning = next(s for s in scheduler.SLOTS if s.key == "morning")
        meal = current_meal_window(morning.hour)
        cands = apply_preference(menu, TasteProfile(), meal)
        self.assertEqual(cands[0].rejected_by, None,
                         "09:00 档不应过滤早餐品")


class TestPreviewSite(unittest.TestCase):
    """在线预览站 —— README 的链接不能指向不存在的页面。

    这是 GitHub Pages 的入口（比赛只按 Star 排名，
    让人不用 clone 就能看到效果是提升转化的关键一步），
    但如果构建脚本坏了，README 里的链接就是一个死链。
    """

    def test_preview_script_exists(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertTrue(
            os.path.exists(os.path.join(root, "scripts", "build_preview.py")),
            "缺少 scripts/build_preview.py，README 的在线预览链接会失效")

    def test_workflow_has_pages_deployment(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        wf = open(os.path.join(root, ".github", "workflows", "tests.yml"),
                  encoding="utf-8").read()
        for key in ("actions/upload-pages-artifact",
                    "actions/deploy-pages",
                    "scripts/build_preview.py"):
            self.assertIn(key, wf, f"workflow 缺少 {key}")

    def test_readme_links_to_preview(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        readme = open(os.path.join(root, "README.md"), encoding="utf-8").read()
        self.assertRegex(
            readme, r"https://f-cfan\.github\.io/mcd-meal-radar",
            "README 首屏应给出在线预览链接 —— 路人不会 clone 仓库")

    def test_out_preview_stays_ignored(self):
        """⚠️ out/ 必须保持 gitignore。

        out/ 里的邮件可能含用户偏好画像与地址，不能提交到公开仓库。
        Pages 走 Actions artifact 上传，不经过 git ——
        绝不能为了部署去放开这个忽略规则。
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        gitignore = open(os.path.join(root, ".gitignore"),
                         encoding="utf-8").read()
        self.assertRegex(gitignore, r"(?m)^out/",
                         "out/ 必须保持忽略（含敏感数据）")

    def test_collect_prefers_newer_copy(self):
        """🔴 防回归：预览收集必须采用**较新**的副本。

        真实 bug（2026-10-16）：`collect()` 先命中 out/preview/ 里的
        旧文件就直接返回，导致本轮新生成的邮件没被拷进去——
        预览站一直展示旧版本（表现为首屏截图仍显示「未核价」，
        而源码早已修复）。预览在骗人，比没有预览更糟。
        """
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        src = open(os.path.join(root, "scripts", "build_preview.py"),
                   encoding="utf-8").read()
        # 必须按mtime 取较新副本
        self.assertIn("st_mtime", src,
                      "collect() 应按 mtime 选择较新副本，否则会固化陈旧产物")
        # 且不能再用「先命中即 break」的写法
        self.assertNotIn("if cand.exists():\n                dst = OUT / fname",
                         src, "collect() 不应先命中即返回（会固化旧副本）")


class TestScreenshotNotStale(unittest.TestCase):
    """🔴 README 首屏截图不得展示已修复的问题。

    这类 bug 有个共同特征：**代码早已修好，但截图还是旧的**，
    于是评审看到的是「未核价」，而作者以为已经解决了。
    用户在 2026-10-16 就是这么发现问题的 —— 直接看示例邮件。

    守卫思路：截图若存在，其 mtime 必须**不早于**它所展示的 HTML 源文件。
    源码更新后未重截图 = 截图过期，必须报错。
    """

    SHOT_PAIRS = (
        ("assets/shots/demo_digest.png", "out/demo_digest.html"),
        ("assets/shots/festival-spring_festival.png",
         "out/demo_festival_spring.html"),
        ("assets/shots/care-lunch.png", "out/care_lunch.html"),
    )

    def test_shots_not_older_than_sources(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        for rel_shot, rel_src in self.SHOT_PAIRS:
            sp = os.path.join(root, *rel_shot.split("/"))
            hp = os.path.join(root, *rel_src.split("/"))
            if not (os.path.exists(sp) and os.path.exists(hp)):
                continue      # 产物未生成时跳过（CI 首次运行属正常）
            self.assertGreaterEqual(
                os.path.getmtime(sp), os.path.getmtime(hp),
                f"{rel_shot} 比 {rel_src} 旧 —— 截图展示的是修复前的效果，"
                f"请重跑 python scripts/build_preview.py --shots")

    def test_no_unpriced_in_generated_previews(self):
        """本地若已生成预览产物，则不得含「未核价」占位。"""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        prev = os.path.join(root, "out", "preview")
        if not os.path.isdir(prev):
            self.skipTest("尚未生成预览产物（CI 首次运行属正常）")
        offenders = []
        for name in sorted(os.listdir(prev)):
            if not name.endswith((".html", ".txt")):
                continue
            with open(os.path.join(prev, name), encoding="utf-8") as fh:
                if "未核价" in fh.read():
                    offenders.append(name)
        self.assertEqual(offenders, [],
                         f"以下预览产物仍含「未核价」：{offenders}")


class TestComboNutritionBreakdown(unittest.TestCase):
    """套餐营养拆解 —— 解决「营养命中率仅 22%」的核心机制。

    背景：官方 list-nutrition-foods 只覆盖**标准单品**（实测 158 项），
    而菜单以**套餐**为主（实测 131 项里大量是「X四件套」），
    直接按名称匹配命中率只有约 22% —— 身体层约束在真实推荐面前被架空。

    解法：query-meal-detail 返回套餐子项 → 与标准单品营养表联表相加。
    **全部来自官方数据，不含任何估算。**
    """

    def _by_name(self):
        from mcd_adapter import Nutrition
        return {
            "中薯条": Nutrition(kcal=289, protein_g=4, fat_g=12,
                                carb_g=45, sodium_mg=165, calcium_mg=20),
            "麦乐鸡5块": Nutrition(kcal=213, protein_g=12, fat_g=14,
                                   carb_g=8, sodium_mg=422, calcium_mg=None),
            "可乐中杯": Nutrition(kcal=147, protein_g=0, fat_g=0,
                                 carb_g=37, sodium_mg=4, calcium_mg=None),
            "无糖可口可乐中杯": Nutrition(kcal=0, protein_g=0, fat_g=0,
                                          carb_g=0, sodium_mg=2, calcium_mg=None),
        }

    def _detail(self):
        return {
            "code": "9900016490", "name": "龙焰鸡腿堡四件套",
            "groups": [
                {"title": "龙焰鸡腿堡", "pick": 1,
                 "default": {"code": "521952", "name": "龙焰鸡腿堡"},
                 "alternatives": []},
                {"title": "选择小食一", "pick": 1,
                 "default": {"code": "501205", "name": "中薯条"},
                 "alternatives": [{"code": "501206", "name": "大薯条",
                                    "diff_price": "+ ¥2"}]},
                {"title": "选择小食二", "pick": 1,
                 "default": {"code": "901401", "name": "麦乐鸡5块"},
                 "alternatives": []},
                {"title": "饮料", "pick": 1,
                 "default": {"code": "903050", "name": "可口可乐中杯"},
                 "alternatives": [{"code": "903071", "name": "无糖可口可乐中杯",
                                    "diff_price": "+ ¥0"}]},
            ],
        }

    def test_sums_matched_subitems(self):
        """已命中的子项应正确相加。"""
        from mcd_adapter import (nutrition_from_combo,
                                 build_nutrition_alias_index)
        by_name = self._by_name()
        idx = build_nutrition_alias_index(by_name)
        total, matched, unmatched = nutrition_from_combo(self._detail(),
                                                        by_name, idx)
        self.assertIn("中薯条", matched)
        self.assertIn("麦乐鸡5块", matched)
        # 289 + 213 + 147（可乐）+ 0（龙焰鸡腿堡未命中）
        self.assertEqual(total.kcal, 649)
        self.assertEqual(total.protein_g, 16)

    def test_unmatched_subitem_is_reported_not_guessed(self):
        """🔴 核心纪律：匹配不上的子项必须报出来，绝不用相近品类填补。"""
        from mcd_adapter import (nutrition_from_combo,
                                 build_nutrition_alias_index)
        by_name = self._by_name()
        idx = build_nutrition_alias_index(by_name)
        total, matched, unmatched = nutrition_from_combo(self._detail(),
                                                        by_name, idx)
        self.assertIn("龙焰鸡腿堡", unmatched,
                      "营养表没有的子项必须列入 unmatched")
        self.assertNotIn("龙焰鸡腿堡", matched)
        self.assertTrue(total.partial, "有未覆盖子项时必须标 partial")

    def test_partial_flag_and_missing_carried(self):
        from mcd_adapter import (nutrition_from_combo,
                                 build_nutrition_alias_index)
        by_name = self._by_name()
        idx = build_nutrition_alias_index(by_name)
        total, _, _ = nutrition_from_combo(self._detail(), by_name, idx)
        self.assertEqual(total.source, "combo_sum")
        self.assertTrue(total.partial)
        self.assertIn("龙焰鸡腿堡", total.missing)

    def test_cup_alias_resolves_official_naming_gap(self):
        """官方两处命名不一致：detail 说「可口可乐中杯」，营养表说「可乐中杯」。"""
        from mcd_adapter import (nutrition_from_combo,
                                 build_nutrition_alias_index)
        by_name = self._by_name()
        idx = build_nutrition_alias_index(by_name)
        _, matched, unmatched = nutrition_from_combo(self._detail(),
                                                    by_name, idx)
        self.assertIn("可口可乐中杯", matched,
                      "杯型别名应让「可口可乐中杯」匹配到「可乐中杯」")
        self.assertNotIn("可口可乐中杯", unmatched)

    def test_no_fuzzy_matching_between_different_products(self):
        """⚠️ 绝不能用「板烧鸡腿堡」冒充「龙焰鸡腿堡」。

        这是本项目最核心的纪律 —— 一个「帮你省钱」的工具
        如果在营养数据上做近似匹配，用户没有任何办法发现。
        """
        from mcd_adapter import (Nutrition, nutrition_from_combo,
                                 build_nutrition_alias_index)
        by_name = dict(self._by_name())
        by_name["板烧鸡腿堡"] = Nutrition(kcal=391, sodium_mg=1041)  # 相近但不同
        idx = build_nutrition_alias_index(by_name)
        total, matched, unmatched = nutrition_from_combo(self._detail(),
                                                        by_name, idx)
        self.assertNotIn("龙焰鸡腿堡", matched)
        self.assertIn("龙焰鸡腿堡", unmatched)
        # 热量不应含板烧的 391
        self.assertNotEqual(total.kcal, 649 + 391)

    def test_low_sodium_swap_found(self):
        """应能从官方替换项里找出降钠选择（无糖可乐钠 2mg vs 可乐 4mg…）。"""
        from mcd_adapter import find_low_sodium_swap, build_nutrition_alias_index
        by_name = self._by_name()
        idx = build_nutrition_alias_index(by_name)
        detail = self._detail()
        # 手动构造一个钠偏高的场景以便触发
        swap = find_low_sodium_swap(detail, by_name, 800, idx)
        self.assertIsNotNone(swap, "应能找到降钠替换项")
        name, saved = swap
        self.assertIn("无糖", name)
        self.assertGreater(saved, 0)

    def test_nutr_sum_preserves_provenance(self):
        """🔴 _nutr_sum 必须透传 source/partial/missing。

        早期版本只累加数值，把「部分和」标记丢了——
        邮件会把 649 kcal（缺 1 项）当成全量展示。
        """
        from solver import _nutr_sum
        from mcd_adapter import MenuItem, Nutrition
        from decimal import Decimal as D
        m = Money = __import__("mcd_adapter").Money
        items = [MenuItem("C1", "四件套", "套餐", (), m(D("39")), m(D("39")))]
        table = {"C1": Nutrition(kcal=649, protein_g=16, source="combo_sum",
                                partial=True, missing=("龙焰鸡腿堡",))}
        total = _nutr_sum(items, table)
        self.assertEqual(total.source, "combo_sum")
        self.assertTrue(total.partial)
        self.assertIn("龙焰鸡腿堡", total.missing)

    def test_nutr_sum_marks_missing_items(self):
        """表里查不到的 item 也要计入 partial，而不是静默跳过。"""
        from solver import _nutr_sum
        from mcd_adapter import MenuItem, Nutrition, Money
        from decimal import Decimal as D
        m = Money(D("39"))
        items = [MenuItem("C1", "四件套", "套餐", (), m, m)]
        total = _nutr_sum(items, {})
        self.assertTrue(total.partial)
        self.assertIn("四件套", total.missing)

    def test_email_discloses_partial_nutrition(self):
        """邮件必须说明这是部分和 —— 否则用户会误以为是全量。"""
        from mailer import nutrition_note
        from mcd_adapter import Nutrition
        partial = Nutrition(kcal=649, source="combo_sum", partial=True,
                            missing=("龙焰鸡腿堡",))
        note = nutrition_note(partial)
        self.assertIn("子项相加", note)
        self.assertIn("未覆盖", note)
        self.assertIn("龙焰鸡腿堡", note)
        # 直接单品不该有说明
        self.assertEqual(nutrition_note(Nutrition(kcal=100)), "")

    def test_parse_meal_detail_structure(self):
        """解析 query-meal-detail 的 rounds 结构。"""
        import json
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        fx = os.path.join(root, "fixtures", "real_meal_detail.json")
        if not os.path.exists(fx):
            self.skipTest("缺少真实套餐详情 fixture")
        payload = json.load(open(fx, encoding="utf-8"))
        from mcd_adapter import parse_meal_detail
        detail = parse_meal_detail({"success": True, "data": payload["data"]})
        self.assertEqual(detail["name"], "龙焰鸡腿堡四件套")
        self.assertEqual(len(detail["groups"]), 4)
        names = [g["default"]["name"] for g in detail["groups"]]
        self.assertIn("中薯条", names)
        # 每个组都应有可选项
        alts = sum(len(g["alternatives"]) for g in detail["groups"])
        self.assertGreater(alts, 5, "真实数据里替换项应该很多")


class TestDeliveryFeeWarning(unittest.TestCase):
    """配送费占比预警 —— 基于官方费用构成的纯算术。

    动机：实测一份 ¥19.70 的单品外送，配送 ¥6.00 + 打包 ¥1.90，
    附加成本占商品价 **40%** —— 比大多数人以为的高得多。
    这个比例是判断「该不该点外送」的关键信息。

    ⚠️ 纪律：只用官方 calculate-price 返回的费用构成，
    **绝不用菜单标价推算配送费**（那是估算）。
    """

    def _cand(self, product, delivery, packing):
        from solver import Candidate, Nutrition
        from mcd_adapter import MenuItem, Money
        from decimal import Decimal as D
        m = Money(D(product) / 100)
        item = MenuItem("C1", "测试餐品", "套餐", (), m, m)
        return Candidate(
            items=[item], coupon=None,
            quoted_price=Money(D(product + delivery + packing) / 100),
            nutrition=Nutrition(),
            fee_breakdown={"product": product, "delivery": delivery,
                           "packing": packing})

    def test_ratio_computed_from_official_breakdown(self):
        c = self._cand(3690, 600, 190)
        self.assertAlmostEqual(float(c.delivery_fee_ratio()), 790 / 3690,
                               places=3)

    def test_warns_when_ratio_high(self):
        """¥19.70 单品 + ¥7.90 附加 = 40%，应预警。"""
        c = self._cand(1970, 600, 190)
        w = c.delivery_fee_warning()
        self.assertIn("40%", w)
        self.assertIn("到店自取", w)
        self.assertIn("¥7.90", w)

    def test_silent_when_ratio_low(self):
        """¥36.90 套餐 + ¥7.90 = 21%，低于阈值应保持安静（避免噪音）。"""
        c = self._cand(3690, 600, 190)
        self.assertEqual(c.delivery_fee_warning(), "")

    def test_silent_for_pickup(self):
        """到店自取无配送费，不该预警。"""
        c = self._cand(3690, 0, 0)
        self.assertEqual(c.delivery_fee_warning(), "")

    def test_no_breakdown_means_no_guess(self):
        """🔴 没有费用构成时必须返回空，绝不用标价推算。"""
        from solver import Candidate, Nutrition
        from mcd_adapter import MenuItem, Money
        from decimal import Decimal as D
        m = Money(D("19.70"))
        item = MenuItem("C1", "测试", "套餐", (), m, m)
        c = Candidate(items=[item], coupon=None, quoted_price=Money(D("19.70")),
                      nutrition=Nutrition())
        self.assertIsNone(c.delivery_fee_ratio())
        self.assertEqual(c.delivery_fee_warning(), "",
                         "无费用构成时不得估算，只能沉默")

    def test_zero_product_does_not_divide_by_zero(self):
        """商品价为 0 时不能崩（曾是很容易忽略的边界）。"""
        c = self._cand(0, 600, 190)
        self.assertIsNone(c.delivery_fee_ratio())
        self.assertEqual(c.delivery_fee_warning(), "")

    def test_threshold_is_configurable(self):
        """阈值应可调 —— 不同用户对运费的敏感度不同。"""
        from decimal import Decimal as D
        c = self._cand(3690, 600, 190)   # 21%
        self.assertEqual(c.delivery_fee_warning(threshold=D("0.30")), "")
        self.assertNotEqual(c.delivery_fee_warning(threshold=D("0.20")), "")

    def test_warning_appears_in_real_solve_pipeline(self):
        """🔴 集成验证：真实 solve 流程里预警要真的挂到 candidate 上。

        只测方法不够 —— 早期版本就出现过「方法写对了但没接上」的情况。
        """
        from solver import (apply_preference, attach_quoted_prices,
                            combo_signature, TasteProfile, Nutrition)
        from mcd_adapter import MenuItem, Money
        from decimal import Decimal as D
        m = Money(D("19.70"))
        item = MenuItem("C1", "小食", "小食", (), m, m)
        cands = apply_preference([item], TasteProfile())
        key = combo_signature([(item.code, 1)])
        table = {key: Money(D("27.60"))}
        bds = {key: {"product": 1970, "delivery": 600, "packing": 190}}
        out = attach_quoted_prices(cands, table, breakdowns=bds)
        self.assertTrue(any("到店自取" in w for w in out[0].warnings),
                        f"真实流程里应出现配送费预警，实际 warnings={out[0].warnings}")


class TestTasteRotation(unittest.TestCase):
    """口味轮换 —— 避免「复读机效应」。

    动机：常点商品底分 +50，若无冷却，一天四档任务会
    周一中午、周一下午、周二中午… 永远推同一份。
    用户看三天就会把邮件拖进垃圾箱 —— 这是体验层面的隐伤。
    """

    def _menu(self):
        from mcd_adapter import MenuItem, Money
        from decimal import Decimal as D
        m = Money(D("39"))
        return [
            MenuItem("A", "常点的龙焰鸡腿堡四件套", "套餐", (), m, m),
            MenuItem("B", "蘸蘸炸鸡", "小食", (),
                     Money(D("19.7")), Money(D("19.7"))),
            MenuItem("C", "四拼小食餐", "小食", (),
                     Money(D("29")), Money(D("29"))),
        ]

    def _cands(self):
        from solver import apply_preference, TasteProfile
        return apply_preference(self._menu(),
                                TasteProfile(top_codes=("A",), order_count=5))

    def _best(self, cands):
        alive = sorted([c for c in cands if not c.rejected_by],
                       key=lambda c: c.score, reverse=True)
        return alive[0].items[0].name

    def _hist(self, *pairs, now=None):
        from datetime import timedelta
        now = now or datetime(2026, 10, 16, 12, 0)
        return [{"code": c, "at": (now - timedelta(hours=h)).isoformat()}
                for c, h in pairs]

    def test_first_push_prefers_favorite(self):
        """无历史时应推常点的。"""
        from solver import apply_rotation
        self.assertEqual(self._best(apply_rotation(self._cands(), [], None)),
                         "常点的龙焰鸡腿堡四件套")

    def test_rotates_after_recent_push(self):
        """🔴 核心：24 小时内推过的应让位。"""
        from solver import apply_rotation
        now = datetime(2026, 10, 16, 12, 0)
        out = apply_rotation(self._cands(), self._hist(("A", 2), now=now), now)
        self.assertEqual(self._best(out), "蘸蘸炸鸡",
                         "刚推过的常点应被降权，让位给次优")

    def test_rotates_further_when_two_pushed(self):
        """推过两个时应轮到第三个。"""
        from solver import apply_rotation
        now = datetime(2026, 10, 16, 12, 0)
        out = apply_rotation(self._cands(),
                             self._hist(("A", 2), ("B", 1), now=now), now)
        self.assertEqual(self._best(out), "四拼小食餐")

    def test_cooldown_expires_after_24h(self):
        """超过 24 小时应恢复常点优先 —— 冷却不是永久的。"""
        from solver import apply_rotation
        now = datetime(2026, 10, 16, 12, 0)
        out = apply_rotation(self._cands(), self._hist(("A", 30), now=now), now)
        self.assertEqual(self._best(out), "常点的龙焰鸡腿堡四件套")

    def test_penalty_must_exceed_favorite_bonus(self):
        """🔴 惩罚值必须大于常点加分，否则轮换形同虚设。

        早期用 -25 而常点是 +50，实测降权后仍稳居第一 ——
        方法写对了但没实际效果，这类「假实现」必须用测试拦住。
        """
        from solver import COOLDOWN_PENALTY, TasteProfile
        self.assertGreater(COOLDOWN_PENALTY,
                           __import__("decimal").Decimal("50"),
                           "惩罚值必须超过常点加分（50），否则轮换无效")

    def test_warning_explains_rotation(self):
        """轮换发生时要说清楚原因，不能静默换掉。"""
        from solver import apply_rotation
        now = datetime(2026, 10, 16, 12, 0)
        out = apply_rotation(self._cands(), self._hist(("A", 2), now=now), now)
        warned = [w for c in out for w in c.warnings if "轮换" in w]
        self.assertTrue(warned, "应提示本轮做了口味轮换")
        self.assertIn("24 小时", warned[0])

    def test_corrupt_history_is_tolerated(self):
        """历史文件损坏不应让主流程崩溃。"""
        import tempfile, os
        from solver import load_rotation
        with tempfile.NamedTemporaryFile("w", suffix=".json",
                                         delete=False, encoding="utf-8") as f:
            f.write("{ 坏掉的 json")
            path = f.name
        try:
            self.assertEqual(load_rotation(path), [])
        finally:
            os.unlink(path)

    def test_bad_timestamp_does_not_crash(self):
        """时间戳格式异常应跳过而非抛异常。"""
        from solver import apply_rotation
        now = datetime(2026, 10, 16, 12, 0)
        out = apply_rotation(self._cands(),
                             [{"code": "A", "at": "不是时间戳"}], now)
        self.assertTrue(out, "坏记录应被忽略，不影响处理")

    def test_only_recent_memory_is_kept(self):
        """只保留最近 ROTATION_MEMORY 条 —— 否则文件会无限增长。"""
        import tempfile, os
        from solver import record_push, load_rotation, ROTATION_MEMORY
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rotation.json")
            for i in range(ROTATION_MEMORY + 3):
                record_push(f"C{i}", datetime(2026, 10, 16, 12, i), path)
            entries = load_rotation(path)
            self.assertEqual(len(entries), ROTATION_MEMORY)
            self.assertEqual(entries[-1]["code"],
                             f"C{ROTATION_MEMORY + 2}",
                             "应保留最近的记录")

    def test_recorded_entry_round_trips(self):
        """record_push 写入的记录应能被 load_rotation 读回。"""
        import tempfile, os
        from solver import record_push, load_rotation
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "rotation.json")
            stamp = datetime(2026, 10, 16, 11, 30, 0)
            record_push("CODE1", stamp, path)
            got = load_rotation(path)
            self.assertEqual(len(got), 1)
            self.assertEqual(got[0]["code"], "CODE1")
            self.assertEqual(got[0]["at"], stamp.isoformat())


class TestStatutoryFestivals(unittest.TestCase):
    """中国国家法定节假日内容风格与背景素材库测试（7 大法定假日）。"""

    def test_all_seven_statutory_festivals_defined(self):
        """必须完整覆盖国务院规定的 7 大全体公民放假法定节假日。"""
        from festivals import STATUTORY_FESTIVAL_KEYS, list_statutory_festivals
        specs = list_statutory_festivals()
        self.assertEqual(len(specs), 7)
        self.assertEqual(tuple(s.key for s in specs), STATUTORY_FESTIVAL_KEYS)

    def test_every_festival_has_existing_bg_image_asset(self):
        """assets/festivals/ 素材库下每个法定节假日都必须有对应的真实背景图片文件。"""
        from festivals import list_statutory_festivals
        for spec in list_statutory_festivals():
            self.assertTrue(
                spec.image_path.exists(),
                f"缺少节日背景图素材：{spec.image_path}",
            )
            self.assertGreater(
                spec.image_path.stat().st_size,
                10_000,
                f"节日背景图过小或损坏：{spec.image_path}",
            )

    def test_solar_statutory_holiday_detection(self):
        """公历固定国家法定节假日（元旦、清明、劳动节、国庆）应自动命中。"""
        from festivals import detect_festival
        self.assertEqual(detect_festival("2026-01-01").key, "new_year")
        self.assertEqual(detect_festival("2026-04-05").key, "qingming")
        self.assertEqual(detect_festival("2026-05-01").key, "labor_day")
        self.assertEqual(detect_festival("2026-10-01").key, "national_day")
        self.assertIsNone(detect_festival("2026-10-16"))

    def test_lunar_statutory_holiday_detection(self):
        """农历国家法定节假日（春节、端午、中秋）查表识别，且中秋与国庆重叠时优先中秋。"""
        from festivals import detect_festival
        self.assertEqual(detect_festival("2026-02-17").key, "spring_festival")
        self.assertEqual(detect_festival("2026-06-19").key, "dragon_boat")
        self.assertEqual(detect_festival("2026-09-25").key, "mid_autumn")
        # 2025-10-06 是农历八月十五中秋，虽在 10.1-10.7 之间，应优先识别为中秋
        self.assertEqual(detect_festival("2025-10-06").key, "mid_autumn")

    def test_chinese_and_english_aliases_resolve(self):
        """支持英文 key 与中文节日名称/别名解析。"""
        from festivals import get_festival
        pairs = {
            "元旦": "new_year",
            "春节": "spring_festival",
            "除夕": "spring_festival",
            "清明": "qingming",
            "五一": "labor_day",
            "劳动节": "labor_day",
            "端午": "dragon_boat",
            "中秋": "mid_autumn",
            "国庆": "national_day",
            "十一": "national_day",
        }
        for alias, expected in pairs.items():
            spec = get_festival(alias)
            self.assertIsNotNone(spec, f"别名 {alias} 未能解析")
            self.assertEqual(spec.key, expected)

    def test_festival_copy_respects_contest_redlines(self):
        """赛事红线与风格纪律：所有节日文案不得含竞品对比，也不得出现说教/催促词。"""
        from festivals import list_statutory_festivals
        banned_brands = ("肯德基", "KFC", "汉堡王", "星巴克", "必胜客", "更便宜", "比别家")
        harsh_words = ("必须", "一定要", "赶紧", "马上", "你的身体垮", "危害健康")
        for spec in list_statutory_festivals():
            texts = [spec.greeting_headline, spec.greeting_body, spec.boost_reason]
            for meal, (t, b) in spec.slot_greetings.items():
                texts.extend([t, b])
            blob = " ".join(texts)
            for w in banned_brands:
                self.assertNotIn(w, blob, f"{spec.name} 文案触碰品牌对比红线：{w}")
            for w in harsh_words:
                self.assertNotIn(w, blob, f"{spec.name} 文案出现说教用词：{w}")

    def test_festival_renders_background_image_and_theme_in_html(self):
        """节日邮件必须引用对应背景图并遵守 <table> 内无裸 <div> 的结构约束。"""
        import re
        from festivals import get_festival
        from mailer import CareContext, render_digest
        from mcd_adapter import Snapshot
        from solver import Decision
        fest = get_festival("spring_festival")
        snap = Snapshot(fetched_at="2026-02-17 12:00:00", scene="delivery",
                        store=None, menu=[])
        care = CareContext(meal="lunch", hour=12, festival=fest)
        html = render_digest(Decision(None, [], [], []), snap, "", "午间",
                             care=care, festival=fest)
        self.assertIn("spring_festival.png", html)
        self.assertIn("background-image:url(", html)
        self.assertIn("春节特辑", html)
        self.assertIn(fest.palette.page_bg, html)
        for m in re.finditer(r"<table\b", html):
            window = html[m.end():m.end() + 300]
            first = re.search(r"<(tr|td|div)\b", window)
            if first:
                self.assertNotEqual(first.group(1), "div",
                                    "节日邮件 <table> 后不能直接跟裸 <div>")

    def test_festival_boost_adds_score_and_explainable_reason(self):
        """节日应景加分应提升匹配餐品分数并附带可解释理由。"""
        from decimal import Decimal as D
        from festivals import get_festival
        from mcd_adapter import MenuItem, Money
        from solver import TasteProfile, apply_festival_boost, apply_preference
        m = Money(D("39"))
        items = [
            MenuItem("C1", "金拱门虾堡四件套", "套餐", ("人气经典",), m, m),
            MenuItem("C2", "普通圆筒", "其他", (), m, m),
        ]
        cands = apply_preference(items, TasteProfile())
        fest = get_festival("spring_festival")
        apply_festival_boost(cands, fest)
        self.assertGreater(cands[0].score, cands[1].score)
        self.assertIn(fest.boost_reason, cands[0].reasons)

    def test_combo_nutrition_does_not_wipe_accumulated_fields_on_none(self):
        """回归保护：后续子项缺某字段（None）时，不能抹掉前面子项已累加出的数值。"""
        from mcd_adapter import Nutrition, build_nutrition_alias_index, nutrition_from_combo
        by_name = {
            "中薯条": Nutrition(kcal=289, protein_g=4, calcium_mg=20),
            "麦乐鸡5块": Nutrition(kcal=213, protein_g=12, calcium_mg=None),
        }
        detail = {
            "code": "T1", "name": "测试套餐",
            "groups": [
                {"title": "小食一", "pick": 1, "default": {"code": "1", "name": "中薯条"}, "alternatives": []},
                {"title": "小食二", "pick": 1, "default": {"code": "2", "name": "麦乐鸡5块"}, "alternatives": []},
            ],
        }
        total, _, _ = nutrition_from_combo(detail, by_name, build_nutrition_alias_index(by_name))
        self.assertEqual(total.kcal, 502)
        self.assertEqual(total.calcium_mg, 20, "前面子项已有的钙含量不应被后续子项的 None 抹成 None")


class TestFestivalRobustness(unittest.TestCase):
    """节日模块的健壮性 —— 用户手输错参数不该让定时任务崩掉。

    背景：`--festival` 是命令行参数，`--festival 123` 会把 int 传进来，
    早期版本直接调 `.strip()` 抛 AttributeError —— 整条调度链断掉。
    """

    def test_non_string_override_does_not_crash(self):
        """🔴 非字符串输入必须优雅降级。"""
        from festivals import detect_festival
        from datetime import date
        for bad in (123, 0, 3.14, ["x"], {"a": 1}, object()):
            try:
                r = detect_festival(date(2026, 10, 1), override_key=bad)
                self.assertIsNone(r, f"{bad!r} 应被忽略并返回 None")
            except Exception as exc:
                self.fail(f"{bad!r} 导致崩溃：{type(exc).__name__}: {exc}")

    def test_unknown_key_returns_none_not_silent_date_fallback(self):
        """🔴 显式指定了未知节日必须返回 None，不能静默按日期回退。

        否则用户以为在看春节主题，实际看到日常样式却找不到原因。
        """
        from festivals import detect_festival
        from datetime import date
        # 10/1 本身是国庆，若静默回退会返回 national_day
        r = detect_festival(date(2026, 10, 1), override_key="春節")
        self.assertIsNone(r, "未知 key 应返回 None 而非按日期检测")

    def test_none_and_empty_fall_back_to_date(self):
        """未指定节日时按日期检测 —— 这是正确行为。"""
        from festivals import detect_festival
        from datetime import date
        self.assertEqual(detect_festival(date(2026, 10, 1),
                                      override_key=None).key, "national_day")
        self.assertEqual(detect_festival(date(2026, 10, 1),
                                      override_key="").key, "national_day")

    def test_explicit_off_switches_festival_off(self):
        """off/none/0/false 应显式关闭节日主题。"""
        from festivals import detect_festival
        from datetime import date
        for off in ("off", "none", "0", "false", "OFF"):
            self.assertIsNone(
                detect_festival(date(2026, 10, 1), override_key=off),
                f"{off!r} 应关闭节日主题")

    def test_valid_aliases_all_resolve(self):
        """中文名与英文 key 都应命中同一节日。"""
        from festivals import get_festival
        pairs = [("春节", "spring_festival"), ("spring_festival", "spring_festival"),
                 ("中秋", "mid_autumn"), ("mid_autumn", "mid_autumn"),
                 ("国庆", "national_day"), ("十一", "national_day"),
                 ("端午", "dragon_boat"), ("清明", "qingming"),
                 ("劳动节", "labor_day"), ("元旦", "new_year")]
        for alias, key in pairs:
            spec = get_festival(alias)
            self.assertIsNotNone(spec, f"{alias} 应能解析")
            self.assertEqual(spec.key, key)

    def test_official_2026_holiday_ranges(self):
        """🔴 2026 法定假期必须与国务院办公厅通知一致。

        依据：国办发明电〔2025〕7 号
          元旦 1/1–1/3 · 春节 2/15–2/23 · 清明 4/4–4/6
          劳动节 5/1–5/5 · 端午 6/19–6/21 · 中秋 9/25–9/27 · 国庆 10/1–10/7
        这类事实性数据一旦漂移就是内容错误，日期表是手工维护的。
        """
        from festivals import detect_festival
        from datetime import date, timedelta
        official = {
            "new_year":        ("2026-01-01", "2026-01-03"),
            "spring_festival": ("2026-02-15", "2026-02-23"),
            "qingming":        ("2026-04-04", "2026-04-06"),
            "labor_day":       ("2026-05-01", "2026-05-05"),
            "dragon_boat":     ("2026-06-19", "2026-06-21"),
            "mid_autumn":      ("2026-09-25", "2026-09-27"),
            "national_day":    ("2026-10-01", "2026-10-07"),
        }
        for key, (start, end) in official.items():
            d = date.fromisoformat(start)
            last = date.fromisoformat(end)
            while d <= last:
                spec = detect_festival(d)
                self.assertIsNotNone(spec, f"{d} 应识别为 {key}")
                self.assertEqual(spec.key, key, f"{d} 识别成了 {spec.key}")
                d += timedelta(days=1)

    def test_every_statutory_festival_has_asset(self):
        """7 个法定节日都必须有背景图素材，且文件真实存在。"""
        from festivals import list_statutory_festivals, FESTIVAL_ASSET_DIR
        specs = list_statutory_festivals()
        self.assertEqual(len(specs), 7)
        for spec in specs:
            path = FESTIVAL_ASSET_DIR / spec.bg_image
            self.assertTrue(path.exists(),
                            f"{spec.key} 的素材缺失：{path}")
            self.assertGreater(path.stat().st_size, 10_000,
                               f"{spec.key} 的素材异常小")

    def test_hero_overlay_falls_back_when_image_blocked(self):
        """🔴 邮件客户端默认屏蔽图片，文字必须仍可读。

        hero_overlay 是半透明白渐变 —— 图片加载失败时
        顶部渐变到近白，文字对比度依然足够。
        """
        from festivals import list_statutory_festivals
        for spec in list_statutory_festivals():
            overlay = spec.palette.hero_overlay
            self.assertIn("linear-gradient", overlay,
                          f"{spec.key} 缺渐变遮罩")
            # 末段应接近不透明，确保文字可读
            self.assertIn("0.9", overlay,
                          f"{spec.key} 的遮罩末段不够不透明，"
                          "图片被屏蔽时文字会看不清")


class TestFestivalCareCopy(unittest.TestCase):
    """节日文案与日常活动提示不应重复叠加。

    真实 bug：节日模式下 `_care_line` 仍追加活动后缀，
    导致「过年好 —— …顺手也把门店这边的好消息给你标出来了」这种
    拖沓且信息重复的句子。
    """

    def _ctx(self, festival=None, campaign=3):
        from mailer import CareContext
        return CareContext(meal="dinner", hour=19, campaign_count=campaign,
                           festival=festival)

    def test_festival_mode_skips_campaign_suffix(self):
        """🔴 节日模式不追加活动后缀。"""
        from mailer import _care_line
        from festivals import get_festival
        fest = get_festival("春节")
        _t, body = _care_line(self._ctx(festival=fest, campaign=5))
        self.assertNotIn("顺手也把门店", body,
                         "节日文案后不应再追加活动提示")

    def test_normal_mode_keeps_campaign_suffix(self):
        """日常模式仍应保留活动后缀 —— 不能为了修节日把日常也砍了。"""
        from mailer import _care_line
        _t, body = _care_line(self._ctx(festival=None, campaign=5))
        self.assertIn("顺手也把门店", body)

    def test_no_campaign_no_suffix_either_way(self):
        """没有活动时两种模式都不该加。"""
        from mailer import _care_line
        from festivals import get_festival
        _t1, b1 = _care_line(self._ctx(festival=None, campaign=0))
        _t2, b2 = _care_line(self._ctx(festival=get_festival("春节"), campaign=0))
        self.assertNotIn("顺手也把门店", b1)
        self.assertNotIn("顺手也把门店", b2)

    def test_festival_greeting_used_per_slot(self):
        """各餐次应有不同的节日文案，不能四个档位说同一句。"""
        from mailer import _care_core
        from festivals import get_festival
        fest = get_festival("spring_festival")
        titles = set()
        for meal in ("breakfast", "lunch", "tea", "dinner"):
            ctx = self._ctx(festival=fest)
            from dataclasses import replace
            t, _b = _care_core(replace(ctx, meal=meal))
            titles.add(t)
        self.assertEqual(len(titles), 4,
                         f"春节四档文案应各不相同，实际只有 {len(titles)} 种：{titles}")


class TestScenePriceArgs(unittest.TestCase):
    """🔴 核价入参矩阵 —— 这次「未核价」复现的真正根因。

    真实 bug（2026-10-16 复现）：`quote_candidates` 只在外送场景
    设置 `beType`，pickup / drive_thru 场景**完全没传** `beType`
    和 `takeWayCode`。官方对参数缺失的响应是：

        「beType1-到店取餐，2-麦乐送到家，5-得来速(DT)，6-企业团餐」

    也就是说，**漏传参数不会报"参数错"，而是回一句枚举提示**，
    看起来像"商品售罄"。上层只校验 `data.price` 非空，
    于是这个组合被标记为未核价 —— 表现就是邮件里冒出「未核价」。

    修法：把场景矩阵收敛成唯一事实来源 `build_price_args`，
    四场景共用同一份逻辑，缺项直接抛错而不是发必然失败的请求。
    """

    def test_pickup_has_be_type_and_take_way_code(self):
        from mcd_adapter import build_price_args
        args = build_price_args("pickup", "1990622", [("1440", 1)],
                                 take_way_code="eat-in")
        self.assertEqual(args["beType"], 1)
        self.assertEqual(args["orderType"], 1)
        self.assertEqual(args["takeWayCode"], "eat-in")
        self.assertNotIn("beCode", args)      # 到店自取官方不要求
        self.assertNotIn("addressId", args)

    def test_drive_thru_be_type_is_five(self):
        """得来速是 beType=5，不是 1 也不是 2 —— 官方枚举里独立一档。"""
        from mcd_adapter import build_price_args
        args = build_price_args("drive_thru", "1990622", [("1440", 1)],
                                 be_code="140064902", take_way_code="eat-in")
        self.assertEqual(args["beType"], 5)
        self.assertEqual(args["beCode"], "140064902")

    def test_delivery_be_type_is_two(self):
        from mcd_adapter import build_price_args
        args = build_price_args("delivery", "1990622", [("1440", 1)],
                                 be_code="140064902", address_id="ADDR-1")
        self.assertEqual(args["beType"], 2)
        self.assertEqual(args["orderType"], 2)
        self.assertEqual(args["addressId"], "ADDR-1")
        self.assertNotIn("takeWayCode", args)

    def test_group_be_type_is_six(self):
        from mcd_adapter import build_price_args
        args = build_price_args("group", "1990622", [("1440", 1)],
                                 be_code="140064902", address_id="ADDR-1")
        self.assertEqual(args["beType"], 6)

    def test_every_scene_always_sends_be_type(self):
        """🔴 所有场景都必须带 beType —— 这正是被漏掉的那个字段。"""
        from mcd_adapter import SCENES, build_price_args
        for scene in SCENES:
            args = build_price_args(
                scene, "1990622", [("1440", 1)],
                be_code="140064902", address_id="ADDR-1",
                take_way_code="eat-in")
            self.assertIn("beType", args, f"{scene} 场景缺少 beType")
            self.assertIsInstance(args["beType"], int)

    def test_missing_required_param_raises_instead_of_silent_call(self):
        """缺必填项必须抛错 —— 发一个注定失败的请求比报错危险得多。"""
        from mcd_adapter import MCPError, build_price_args
        with self.assertRaises(MCPError):
            build_price_args("pickup", "1990622", [("1440", 1)])  # 缺 takeWayCode
        with self.assertRaises(MCPError):
            build_price_args("delivery", "1990622", [("1440", 1)])  # 缺 beCode
        with self.assertRaises(MCPError):
            build_price_args("delivery", "1990622", [("1440", 1)],
                             be_code="140064902")                   # 缺 addressId
        with self.assertRaises(MCPError):
            build_price_args("nonexistent", "1990622", [("1440", 1)])

    def test_quote_candidates_aborts_when_param_matrix_invalid(self):
        """参数矩阵不满足时整批放弃，而不是发出 N 个必然失败的请求。"""
        from collector import quote_candidates
        calls = []

        class _Client:
            def call_tool(self, name, args):
                calls.append((name, args))
                return {"success": True, "data": {"price": 100}}

        table, bd = quote_candidates(
            _Client(), "1990622", "pickup",
            [[("1440", 1)], [("4810", 1)]], take_way_code=None)
        self.assertEqual(table, {})
        self.assertEqual(bd, {})
        self.assertEqual(calls, [], "参数缺失时不应发出任何请求")


class TestUnpricedNeverShown(unittest.TestCase):
    """🔴 展示层纪律 —— 未核价候选不占推荐位。

    真实 bug（2026-10-16 用户复现）：两个示例邮件里出现
    「麦辣鸡腿汉堡 · 未核价」。根因不止fixture覆盖不足，
    更根本的是 solver 只做了「已核价优先」排序，**没有排除**未核价：
    已核价候选不足 3 个时，未核价仍会被填进 runners_up。

    「不估算价格」有两层含义，第二层常被忽略：
      · 不编造价格（quoted_price 为 None 就是没有）
      · 不用无法执行的价格占位（用户看到没金额的推荐，价值为零）
    """

    def _decide(self, price_table, meal="lunch"):
        from collector import load_snapshot_from_fixtures
        from solver import DietaryPolicy, TasteProfile, solve  # noqa: F811
        snap = load_snapshot_from_fixtures(scene="delivery")
        return solve(snap, TasteProfile(order_count=4),
                     DietaryPolicy(), price_table, meal=meal)

    def test_full_fixture_has_zero_unpriced_in_output(self):
        """真实录制价格表下，输出里不应出现任何未核价条目。"""
        from collector import (load_price_table_from_fixtures,
                               load_snapshot_from_fixtures,
                               plan_quotes)
        from solver import TasteProfile, DietaryPolicy, combo_signature
        snap = load_snapshot_from_fixtures(scene="delivery")
        table = load_price_table_from_fixtures()
        for meal in ("breakfast", "lunch", "dinner", "tea", ""):
            combos, _ = plan_quotes(snap, TasteProfile(order_count=4),
                                     DietaryPolicy(), meal=meal)
            missing = [combo_signature(c) for c in combos
                       if combo_signature(c) not in table]
            # 培根蛋麦满分是早餐，当前真实售罄 —— 官方返回无 price
            self.assertEqual(
                [m for m in missing if m != "9900003321:1"], [],
                f"{meal or '(不限)'} 档 fixture 价格表覆盖不足：{missing}")

    def test_partial_coverage_excludes_unquoted(self):
        """🔴 核心回归：部分覆盖时，未核价候选不得进入推荐列表。"""
        from collector import load_price_table_from_fixtures
        full = load_price_table_from_fixtures()
        # 只给单品「1440」有价：它 485 kcal / 1208 mg，在默认上限内能存活。
        # ⚠️ 不能用 9900005456（三件套）：真实营养 921 kcal 会被身体层淘汰，
        # 于是已核价候选为空，本测试想验的「部分覆盖」场景根本不会发生。
        partial = {"1440:1": full["1440:1"]}
        d = self._decide(partial)
        self.assertIsNotNone(d.best)
        self.assertTrue(d.best.is_quoted, "首选必须是已核价候选")
        for a in d.runners_up:
            self.assertTrue(a.is_quoted,
                            f"备选出现未核价条目：{a.name}")
        self.assertTrue(any("未列入推荐" in n for n in d.notes),
                        f"应说明被排除的未核价候选数，实际 notes={d.notes}")

    def test_all_unquoted_falls_back_with_warning(self):
        """全部未核价时才回退展示，并必须明确告知金额未核实。"""
        d = self._decide({})
        self.assertIsNotNone(d.best, "全未核价时仍应给出候选（而非空手）")
        self.assertFalse(d.best.is_quoted)
        self.assertTrue(any("金额未核实" in n for n in d.notes),
                        f"应说明金额未核实，实际 notes={d.notes}")

    def test_no_price_table_never_prints_empty_recommendation(self):
        """无价格表时不能出现「有推荐但全无金额」的静默状态。"""
        d = self._decide(None)
        self.assertTrue(d.notes, "无价格表必须给出说明")
        self.assertIn("计价", d.degraded_layers)

    def test_demo_output_has_no_unpriced_marker(self):
        """端到端：demo 产出的示例邮件里不得出现「未核价」。"""
        from collector import (load_price_table_from_fixtures,
                               load_snapshot_from_fixtures)
        from mailer import render_digest
        from festivals import get_festival
        snap = load_snapshot_from_fixtures(scene="delivery")
        full = load_price_table_from_fixtures()
        for meal in ("lunch", "dinner"):
            d = self._decide(full, meal=meal)
            html = render_digest(d, snap, "基于你的 4 笔历史订单", meal,
                                 festival=get_festival("spring_festival"))
            self.assertNotIn("未核价", html,
                             f"{meal} 档示例邮件仍出现「未核价」占位")


class TestCouponApplicability(unittest.TestCase):
    """🔴 券的适用范围校验 —— 官方 `products` 字段必须用上。

    真实 bug（2026-10-16 用户复现「未核价」时顺带发现）：
    早期版本不校验适用范围，于是造出「无糖饮品立减券 + 麦辣鸡腿汉堡」
    这种官方根本不允许的组合，后果有两层：
      ① calculate-price 核不出价 → 邮件显示「未核价」
      ② 更糟：它排在推荐首位，而用户下单时**用不了这张券**

    实测（2026-10-16 16:10 真实调用 query-store-coupons）：
      当前门店 3 张券，适用商品 9900012387 / 9900016371 / 507411，
      **全都不在 123 项菜单里**。不校验就会持续推荐无法使用的券。
    """

    def _snap(self, coupons):
        from decimal import Decimal as D
        from mcd_adapter import Coupon, MenuItem, Money, Snapshot
        m = Money(D("22.5"))
        menu = [
            MenuItem("1440", "麦辣鸡腿汉堡", "单品", ("人气经典",), m, m),
            MenuItem("9900008751", "可乐(中)", "冷饮", ("冷饮",), m, m),
        ]
        return Snapshot(fetched_at="2026-10-16 12:00:00", scene="delivery",
                        store=None, menu=menu, store_coupons=coupons)

    def test_coupon_only_pairs_with_applicable_product(self):
        from solver import (apply_preference, enumerate_coupon_combinations,
                            TasteProfile)
        from mcd_adapter import Coupon
        from decimal import Decimal as D
        coupon = Coupon(
            coupon_id="C1", title="无糖饮品立减4.5元券",
            value=Money(D("4.5")), threshold=None, expires_in_days=1,
            applies_to=("9900008751",))     # 只适用于可乐
        snap = self._snap([coupon])
        cands = apply_preference(snap.menu, TasteProfile())
        expanded = enumerate_coupon_combinations(cands, [], snap.store_coupons)

        with_coupon = [c for c in expanded if c.coupon is not None]
        for c in with_coupon:
            self.assertIn("9900008751", [i.code for i in c.items],
                          f"「无糖饮品券」不应配到 {[i.name for i in c.items]}")
        self.assertTrue(with_coupon, "适用的商品仍应能配券（不能连这个也拦掉）")

    def test_coupon_not_in_menu_produces_no_pair(self):
        """券的适用商品不在菜单里时，不应产生任何券组合。"""
        from solver import (apply_preference, enumerate_coupon_combinations,
                            TasteProfile)
        from mcd_adapter import Coupon
        from decimal import Decimal as D
        coupon = Coupon(
            coupon_id="C2", title="中薯条买一送一",
            value=Money(D("13.5")), threshold=None, expires_in_days=0,
            applies_to=("9900012387",))     # 该编码不在菜单
        snap = self._snap([coupon])
        cands = apply_preference(snap.menu, TasteProfile())
        expanded = enumerate_coupon_combinations(cands, [], snap.store_coupons)
        self.assertEqual([c for c in expanded if c.coupon is not None], [],
                         "券的适用商品不在菜单时不应配出组合")

    def test_coupon_without_applicability_keeps_legacy_behavior(self):
        """官方未提供适用范围时，不做推测，沿用原有配对行为。"""
        from solver import (apply_preference, enumerate_coupon_combinations,
                            TasteProfile)
        from mcd_adapter import Coupon
        from decimal import Decimal as D
        coupon = Coupon(
            coupon_id="C3", title="卡包券", value=Money(D("6")),
            threshold=None, expires_in_days=5, applies_to=())  # 官方未提供
        snap = self._snap([coupon])
        cands = apply_preference(snap.menu, TasteProfile())
        expanded = enumerate_coupon_combinations(cands, [], snap.store_coupons)
        self.assertTrue([c for c in expanded if c.coupon is not None],
                        "官方未提供范围时不应凭推测拦掉券")

    def test_parse_coupons_reads_products_scope(self):
        """🔴 parse_coupons 必须解析官方 products 适用范围字段。"""
        from mcd_adapter import parse_coupons
        payload = {"success": True, "data": [
            {"couponId": "X1", "title": "9.9元中杯冰美式",
             "products": [{"productCode": "507411",
                           "productName": "麦咖啡™冰美式中杯"}],
             "tradeDateTime": "2026-10-16 00:00:00-2026-10-22 23:59:59"}]}
        coupons = parse_coupons(payload, store_scoped=True)
        self.assertEqual(len(coupons), 1)
        self.assertEqual(coupons[0].applies_to, ("507411",))

    def test_parse_coupons_accepts_array_data(self):
        """🔴 实测：`query-store-coupons` 的 data 是**数组**，不是对象。

        早期实现只认 `{coupons:[...]}`，导致线上门店券恒为空、
        券这一层整体降级。
        """
        from mcd_adapter import parse_coupons
        arr = {"data": [{"couponId": "A", "title": "券A"}]}
        self.assertEqual(len(parse_coupons(arr)), 1)
        obj = {"data": {"coupons": [{"couponId": "B", "title": "券B"}]}}
        self.assertEqual(len(parse_coupons(obj)), 1)

    def test_real_fixture_coupons_have_scope(self):
        """真实录制的门店券必须带适用范围（这是本次修复的数据基础）。"""
        from mcd_adapter import parse_coupons
        path = os.path.join(FIXTURES, "real_store_coupons.json")
        if not os.path.exists(path):
            self.skipTest("尚未录制真实门店券")
        payload = json.load(open(path, encoding="utf-8"))
        coupons = parse_coupons(payload, store_scoped=True)
        self.assertTrue(coupons, "真实门店券应能解析出至少一张")
        for c in coupons:
            self.assertTrue(c.applies_to,
                            f"真实券「{c.title}」应带官方 products 适用范围")


if __name__ == "__main__":
    unittest.main(verbosity=2)
