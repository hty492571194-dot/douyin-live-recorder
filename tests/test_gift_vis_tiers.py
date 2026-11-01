"""礼物可见度档位表(方案C,2026-10-30)的数值与视觉守卫。

2026-10-30 的起因:历史页胶囊的 0 与 1/4、3/4 与 4/4 颜色完全一致,用户扫不出区别。
根因不是"配色不好",是**图例手写在弹窗 HTML、颜色写在 CSS 的 lvN 类里**,
和胶囊各写一份;3/4 那档还只靠 `opacity: .75` 与 4/4 区分(实测 ΔE00 = 12)。

现在档位表是唯一真源(`static/index.html` 里的 `<script id="gift-vis-tiers">` JSON),
行内胶囊、弹窗图例、tooltip 文案、阈值判定全部由它派生。这里守三件事:
  1. 表自身合法:区间连续不重叠、覆盖 (0,1]、label 是百分比说法、每档有说明;
  2. 颜色两两不同,且**相邻两档的色差 ΔE00 ≥ 15**(比"看着差不多"更硬的判据);
  3. 渲染函数真的按表来:颜色从表注入、圆点只在顶档且已对账、格数按四分位取整。

    .venv/bin/python -m unittest tests.test_gift_vis_tiers
"""
import json
import math
import os
import re
import unittest

import webui

BASE = webui.BASE
HTML_REL = os.path.join("static", "index.html")


def _html():
    with open(os.path.join(BASE, HTML_REL), encoding="utf-8") as f:
        return f.read()


def _tiers():
    m = re.search(r'<script id="gift-vis-tiers" type="application/json">(.*?)</script>',
                  _html(), re.S)
    if not m:
        raise AssertionError("档位表 JSON 段缺失(#gift-vis-tiers)")
    return json.loads(m.group(1))


# ── CIEDE2000:判断"两档颜色是不是看着一样"用,和设计稿里的口径一致 ──
def _srgb2lin(c):
    c /= 255.0
    return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4


def _hex2lab(h):
    h = h.lstrip("#")
    r, g, b = [_srgb2lin(int(h[i:i + 2], 16)) for i in (0, 2, 4)]
    x = r * 0.4124 + g * 0.3576 + b * 0.1805
    y = r * 0.2126 + g * 0.7152 + b * 0.0722
    z = r * 0.0193 + g * 0.1192 + b * 0.9505

    def f(t):
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    fx, fy, fz = f(x / 0.95047), f(y / 1.0), f(z / 1.08883)
    return (116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz))


def _de00(c1, c2):
    l1, a1, b1 = _hex2lab(c1)
    l2, a2, b2 = _hex2lab(c2)
    c_1, c_2 = math.hypot(a1, b1), math.hypot(a2, b2)
    cb = (c_1 + c_2) / 2
    g = 0.5 * (1 - math.sqrt(cb ** 7 / (cb ** 7 + 25 ** 7)))
    a1p, a2p = (1 + g) * a1, (1 + g) * a2
    c1p, c2p = math.hypot(a1p, b1), math.hypot(a2p, b2)
    h1p = math.degrees(math.atan2(b1, a1p)) % 360
    h2p = math.degrees(math.atan2(b2, a2p)) % 360
    dlp, dcp = l2 - l1, c2p - c1p
    if c1p * c2p == 0:
        dhp = 0
    elif abs(h2p - h1p) <= 180:
        dhp = h2p - h1p
    elif h2p - h1p > 180:
        dhp = h2p - h1p - 360
    else:
        dhp = h2p - h1p + 360
    dhp_ = 2 * math.sqrt(c1p * c2p) * math.sin(math.radians(dhp) / 2)
    lbp, cbp = (l1 + l2) / 2, (c1p + c2p) / 2
    if c1p * c2p == 0:
        hbp = h1p + h2p
    elif abs(h1p - h2p) <= 180:
        hbp = (h1p + h2p) / 2
    elif h1p + h2p < 360:
        hbp = (h1p + h2p + 360) / 2
    else:
        hbp = (h1p + h2p - 360) / 2
    t = (1 - 0.17 * math.cos(math.radians(hbp - 30))
         + 0.24 * math.cos(math.radians(2 * hbp))
         + 0.32 * math.cos(math.radians(3 * hbp + 6))
         - 0.20 * math.cos(math.radians(4 * hbp - 63)))
    dth = 30 * math.exp(-(((hbp - 275) / 25) ** 2))
    rc = 2 * math.sqrt(cbp ** 7 / (cbp ** 7 + 25 ** 7))
    sl = 1 + (0.015 * (lbp - 50) ** 2) / math.sqrt(20 + (lbp - 50) ** 2)
    sc, sh = 1 + 0.045 * cbp, 1 + 0.015 * cbp * t
    rt = -math.sin(math.radians(2 * dth)) * rc
    return math.sqrt((dlp / sl) ** 2 + (dcp / sc) ** 2 + (dhp_ / sh) ** 2
                     + rt * (dcp / sc) * (dhp_ / sh))


class TestTierTable(unittest.TestCase):
    """档位表自身的合法性。"""

    def setUp(self):
        self.tiers = _tiers()

    def test_six_tiers_from_zero_to_five(self):
        self.assertEqual([t["tier"] for t in self.tiers], [0, 1, 2, 3, 4, 5],
                         "档位必须 0~5 连续,缺档会让 giftVisOf 落空")

    def test_bands_are_contiguous_and_cover_unit_interval(self):
        """区间首尾相接、不重叠、正好覆盖 (0,1]。"""
        ts = self.tiers
        self.assertEqual(ts[0]["min"], 0)
        self.assertEqual(ts[0]["max"], 0, "0 档只管完整率 0(平台未下发)")
        self.assertEqual(ts[1]["min"], 0, "1 档从 0 开始(0 以上都属于它)")
        for prev, cur in zip(ts[1:], ts[2:]):
            self.assertEqual(cur["min"], prev["max"],
                             f"档位 {prev['tier']}→{cur['tier']} 之间有缝或重叠")
        self.assertEqual(ts[-1]["max"], 1, "顶档必须封到 1.0")
        for t in ts:
            self.assertLessEqual(t["min"], t["max"], f"档位 {t['tier']} 区间倒挂")

    def test_top_tier_threshold_is_95_percent(self):
        top = self.tiers[-1]
        self.assertAlmostEqual(top["min"], 0.95, places=6,
                               msg="顶档门槛 = 95%(用户明确要求的那一档)")
        self.assertTrue(top["dot"], "顶档必须靠圆点标记(4 格刻度区分不出它和 4/4)")

    def test_only_top_tier_has_dot(self):
        dots = [t["tier"] for t in self.tiers if t.get("dot")]
        self.assertEqual(dots, [5], "圆点只能属于顶档,多给一个就贬值了")

    def test_labels_are_percent_not_fractions(self):
        """label 一律百分比说法(2026-10-30 用户要求去掉 1/4、2/4)。"""
        for t in self.tiers[1:]:
            self.assertIn("%", t["label"], f"档位 {t['tier']} 的 label 必须是百分比")
            self.assertNotRegex(t["label"], r"\d\s*/\s*[2-4]",
                                f"档位 {t['tier']} 还在用分数刻度: {t['label']}")
        self.assertEqual(self.tiers[0]["label"], "无数据")

    def test_every_tier_has_desc_and_range(self):
        for t in self.tiers:
            self.assertTrue(t.get("desc", "").strip(), f"档位 {t['tier']} 缺 desc")
            self.assertTrue(t.get("range", "").strip(), f"档位 {t['tier']} 缺 range")

    def test_colors_unique_and_adjacent_readable(self):
        """颜色两两不同,且相邻两档 ΔE00 ≥ 15 —— 这次 bug 的直接守卫。

        旧实现最差一对:图例里 0 与 1/4 ΔE00 = 0(同一个红点)、
        行内 3/4 与 4/4 = 12(opacity 混色后几乎同色)。
        """
        cs = [t["color"] for t in self.tiers]
        self.assertEqual(len(set(cs)), len(cs), f"有档位共用颜色: {cs}")
        for prev, cur in zip(self.tiers, self.tiers[1:]):
            d = _de00(prev["color"], cur["color"])
            self.assertGreaterEqual(
                round(d, 1), 15.0,
                f"档位 {prev['tier']}({prev['color']}) 与 {cur['tier']}({cur['color']}) "
                f"色差只有 ΔE00={d:.1f},一眼分不出来")

    def test_reuses_theme_colors(self):
        """颜色要和全站主题对得上(灰/红/琥珀/绿四个取自 :root 变量)。"""
        html = _html()
        for var, want in (("--danger", "#f87171"), ("--warn", "#fbbf24"), ("--ok", "#34d399")):
            self.assertIn(f"{var}: {want}", html, f"主题色 {var} 变了,档位表要跟着改")
        colors = {t["color"] for t in self.tiers}
        for want in ("#f87171", "#fbbf24", "#34d399"):
            self.assertIn(want, colors, f"档位表应复用主题色 {want}")


class TestTierRender(unittest.TestCase):
    """渲染函数真的按表来(文本级守卫,前端没有 JS 运行时)。"""

    def setUp(self):
        self.html = _html()

    def test_threshold_comes_from_table_not_hardcoded(self):
        """顶档门槛必须从表里取,不许在 giftVisOf 里写死 —— 否则改表不生效。"""
        m = re.search(r"function giftVisOf\([\s\S]*?\n\}\n", self.html)
        self.assertTrue(m, "giftVisOf 缺失")
        body = m.group(0)
        self.assertIn("topMin", body, "顶档门槛应取档位表最后一档的 min")
        self.assertNotRegex(body, r"r\s*[<>]=?\s*0\.9[0-9]",
                            "giftVisOf 里不许写死 0.95 门槛(改表要能生效)")

    def test_color_injected_from_table(self):
        """胶囊颜色由表注入(style),不再走 CSS 的 lvN 类。"""
        self.assertIn('style="color:${t.color}"', self.html,
                      "胶囊颜色必须从档位表注入")
        for n in range(6):
            self.assertNotIn(f".g-vis.lv{n}", self.html,
                             f"CSS 里不许再有 .g-vis.lv{n} 颜色类(会与表脱钩)")

    def test_dot_requires_verified(self):
        """圆点 = 顶档 + 已对账;未对账(field_only/旧口径)一律不发。"""
        self.assertIn("if (t.dot && v.verified)", self.html,
                      "圆点必须同时看档位与对账状态")
        self.assertIn('status !== "field_only"', self.html,
                      "field_only(补算场次)必须判为未对账")

    def test_zero_is_split_three_ways(self):
        """0 值必须三分:平台未下发 / 全丢(真故障) / 旧口径无法判定。"""
        for why in ("channel_off", "all_lost", "legacy_zero"):
            self.assertIn(f'"{why}"', self.html, f"缺少 0 值分类 {why}")
        self.assertIn('why: "all_lost"', self.html,
                      "收到了却全丢必须是独立分支(旧实现判成灰色 0 档)")

    def test_cells_follow_quarters(self):
        """格数按四分位取整,与档位区间天然对齐((0,25%]→1 格 … (75%,100%]→4 格)。"""
        self.assertIn("Math.ceil(r * 4)", self.html, "格数必须按四分位取整")

    def test_pct_never_shows_zero_for_tiny_ratio(self):
        """0.4% 不许显示成 0%(会与"0 格"自相矛盾)。"""
        self.assertIn("Math.max(1, Math.round(r * 100))", self.html,
                      "百分比下限必须是 1%")

    def test_legend_is_generated_from_table(self):
        """弹窗图例由表渲染 —— 手写图例正是这次"两对同色"的根因。"""
        self.assertIn('id="gvc-legend"', self.html, "图例容器缺失")
        self.assertIn("function renderGiftVisLegend", self.html, "图例渲染函数缺失")
        self.assertIn("renderGiftVisLegend();", self.html, "图例没有初始化")
        seg = self.html[self.html.index('id="gift-vis-modal"'):self.html.index('id="gift-vis-tiers"')]
        self.assertNotIn("h-dot", seg,
                         "图例里不许再手写 h-dot(必须由档位表渲染)")


if __name__ == "__main__":
    unittest.main()
