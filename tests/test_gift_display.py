#!/usr/bin/env python3
"""礼物展示规则 / 调度 / 版式 / 实时语义 的单元测试。

对应《字幕礼物展示限流部署方案-竖屏版-2026-10-29》§11：
  · 规则：98/99 边界、已知 0 与未知价格、full 合并取 max、light 不合并、份数升级、优先级
  · 调度：稳定前缀、事件恰好一次、跨片不重复、片尾溢出、借槽不改语义
  · 版式：portrait-v1 的 scale/字号/槽位/底部；横屏回退旧路径
  · 实时：display 文案/车道/合并键语义
  · 黄金样例：录制端与下游共用的唯一基线

夹具一律合成（主播A / 观众甲乙 / 合成 gift_id），不写真实主播或观众。
"""
import json
import os
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import gift_display as gd          # noqa: E402
import subtitle                    # noqa: E402

ORIGIN = 1793200000000
DICT_RAW = {
    "1001": {"name": "小心心", "diamond": 1},
    "1002": {"name": "闪烁星河", "diamond": 98},
    "1003": {"name": "亲吻", "diamond": 99},
    "1004": {"name": "嘉年华", "diamond": 30000},
    "1005": {"name": "免费祝福", "diamond": 0},
}


class _Dict:
    def __init__(self, raw=None):
        self.raw = {int(k): v for k, v in (raw or DICT_RAW).items()}
        self.by_name = {}
        for gid, info in self.raw.items():
            self.by_name.setdefault(info["name"], []).append(gid)

    def info(self, gid):
        return self.raw.get(int(gid))

    def find_by_name(self, name):
        return list(self.by_name.get(name, ()))


def ev(off, gid, dia, cnt=1, uid="", user="", ch="light"):
    return {"ts": ORIGIN + off, "type": "gift", "gift": f"礼物#{gid}",
            "diamond": dia, "count": cnt, "uid": uid, "user": user, "channel": ch}


def items_of(events, cfg=None, dct=None):
    return gd.shift_items(gd.build_display_items(events, cfg, dct or _Dict()), ORIGIN)


def one_seg(end_ms=60000):
    return [{"idx": 0, "start_ms": 0, "end_ms": end_ms}]


class TestLaneBoundary(unittest.TestCase):
    """98 进快、99 进慢；已知 0 与未知价格分开处理。"""

    def test_boundary_97_98_99(self):
        self.assertEqual(gd.decide_lane(97, True), gd.FAST)
        self.assertEqual(gd.decide_lane(98, True), gd.FAST)
        self.assertEqual(gd.decide_lane(99, True), gd.SLOW)

    def test_known_zero_is_not_unknown(self):
        """官方字典明确 0 抖币 → 已知价格，走快车道。"""
        self.assertEqual(gd.decide_lane(0, True), gd.FAST)
        self.assertTrue(gd.normalize_event(ev(0, 1005, 0), None, _Dict())["price_known"])

    def test_unknown_price_goes_slow(self):
        ne = gd.normalize_event({"ts": ORIGIN, "type": "gift", "gift": "礼物#9999",
                                 "diamond": 0, "count": 1}, None, _Dict())
        self.assertFalse(ne["price_known"])
        self.assertEqual(ne["lane"], gd.SLOW)

    def test_unknown_text_marks_question(self):
        ne = gd.normalize_event({"ts": ORIGIN, "type": "gift", "gift": "礼物#9999",
                                 "diamond": 0, "count": 1, "uid": "u", "user": "甲",
                                 "channel": "full"}, None, _Dict())
        self.assertIn("? 抖币", gd.render_text(ne))


class TestMerge(unittest.TestCase):
    """full 合并取累计最大值、3 秒窗、超窗另起；light 永不合并。"""

    def test_full_combo_merges_with_max(self):
        evs = [ev(0, 1001, 1, 1, uid="u1", user="甲", ch="full"),
               ev(1000, 1001, 1, 5, uid="u1", user="甲", ch="full"),
               ev(2000, 1001, 1, 9, uid="u1", user="甲", ch="full")]
        items = items_of(evs)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["count"], 9, "累计 count 必须取最大值而非累加")
        self.assertTrue(items[0]["merged"])
        self.assertEqual(items[0]["source_event_indexes"], [0, 1, 2])

    def test_full_combo_segment_texts_grow(self):
        evs = [ev(0, 1001, 1, 1, uid="u1", user="甲", ch="full"),
               ev(1000, 1001, 1, 5, uid="u1", user="甲", ch="full"),
               ev(2000, 1001, 1, 9, uid="u1", user="甲", ch="full")]
        segs = items_of(evs)[0]["segments"]
        self.assertEqual([s["text"].split("×")[1].split("（")[0] for s in segs],
                         ["1", "5", "9"])
        self.assertLessEqual(len(segs), gd.DEFAULTS["max_segments"])

    def test_beyond_window_starts_new_item(self):
        evs = [ev(0, 1001, 1, 1, uid="u1", user="甲", ch="full"),
               ev(9000, 1001, 1, 12, uid="u1", user="甲", ch="full")]
        self.assertEqual(len(items_of(evs)), 2)

    def test_different_people_never_merge(self):
        evs = [ev(0, 1001, 1, 1, uid="u1", user="甲", ch="full"),
               ev(1000, 1001, 1, 1, uid="u2", user="乙", ch="full")]
        self.assertEqual(len(items_of(evs)), 2)

    def test_light_never_merges(self):
        """三个不同人各送 3 个小心心 = 9 个事件 → 必须 9 个独立展示项。"""
        evs = []
        for uid in ("u1", "u2", "u3"):
            for k in range(3):
                evs.append(ev(k * 500, 1001, 1, 1, uid=uid, user=uid, ch="light"))
        items = items_of(evs)
        self.assertEqual(len(items), 9)
        self.assertEqual(len({it["key"] for it in items}), 9, "light 的 key 必须唯一")
        self.assertTrue(all(not it["merged"] for it in items))

    def test_light_text_has_no_nickname(self):
        self.assertEqual(items_of([ev(0, 1001, 1)])[0]["segments"][0]["text"], "小心心 ×1")


class TestPromoteAndPriority(unittest.TestCase):
    """count>=10 升级视觉车道与优先级；P0 只优先占后续空槽。"""

    def test_count_promotes_to_slow_visual(self):
        it = items_of([ev(0, 1001, 1, 10)])[0]
        self.assertEqual(it["lane"], gd.FAST, "价格车道仍是快车道")
        self.assertEqual(it["visual_lane"], gd.SLOW, "份数达标升级视觉慢车道")
        self.assertEqual(it["priority"], gd.P1)

    def test_priority_classes(self):
        self.assertEqual(gd.priority_of(30000, True, 1), gd.P0)
        self.assertEqual(gd.priority_of(99, True, 1), gd.P1)
        self.assertEqual(gd.priority_of(1, True, 1), gd.P2)

    def test_p0_does_not_preempt_started_item(self):
        evs = [ev(0, 1001, 1, 1),            # 快车道占槽
               ev(100, 1004, 30000, 1, uid="u2", user="乙", ch="full")]
        plan = gd.plan_session_display(items_of(evs), one_seg())
        rows = plan["segments"][0]["items"]
        fast = [r for r in rows if r["lane"] == gd.FAST][0]
        p0 = [r for r in rows if r["priority"] == gd.P0][0]
        self.assertLessEqual(fast["start_ms"], p0["start_ms"])
        self.assertEqual(p0["delay_ms"], 0, "有空槽时 P0 不该等")


class TestScheduling(unittest.TestCase):
    """全场调度：事件恰好一次、不跨片、片尾溢出、借槽、稳定前缀。"""

    def test_each_source_event_exactly_once(self):
        evs = [ev(i * 400, 1001, 1) for i in range(10)]
        evs += [ev(5000 + i * 100, 1004, 30000, 1, uid=f"u{i}", user=f"乙{i}", ch="full")
                for i in range(3)]
        items = items_of(evs)
        plan = gd.plan_session_display(items, one_seg())
        seen = []
        for row in plan["segments"][0]["items"]:
            seen += row["source_event_indexes"]
        for o in plan["tail_overflow"]:
            seen += o["source_event_indexes"]
        self.assertEqual(sorted(seen), list(range(len(evs))), "每个原始事件必须恰好出现一次")
        self.assertTrue(plan["stats"]["covered"])

    def test_items_never_cross_segment(self):
        evs = [ev(3900, 1004, 30000, 1, uid="u1", user="甲", ch="full"),
               ev(4000, 1003, 99, 1)]
        segs = [{"idx": 0, "start_ms": 0, "end_ms": 4000},
                {"idx": 1, "start_ms": 4000, "end_ms": 60000}]
        plan = gd.plan_session_display(items_of(evs), segs)
        for s in plan["segments"]:
            for row in s["items"]:
                self.assertEqual(row["seg"], s["idx"])
                self.assertLessEqual(row["end_ms"], s["end_ms"])

    def test_last_segment_shortfall_goes_overflow(self):
        evs = [ev(0, 1004, 30000, 1, uid="u1", user="甲", ch="full")]
        plan = gd.plan_session_display(items_of(evs), one_seg(end_ms=1000))
        self.assertEqual(len(plan["tail_overflow"]), 1)
        self.assertEqual(plan["tail_overflow"][0]["reason"], "tail_video_end")
        self.assertEqual(plan["stats"]["overflow_events"], 1)

    def test_borrow_slot_keeps_style(self):
        """慢车道偏好槽（默认 3 个）占满后借快车道槽：样式/停留不变，只多 borrowed_slot 标记。"""
        n = int(gd.DEFAULTS["slow_preferred_slots"]) + 1     # 比偏好槽多一个才会借
        evs = [ev(i * 100, 1003, 99) for i in range(n)]
        items = items_of(evs)
        plan = gd.plan_session_display(items, one_seg())
        rows = plan["segments"][0]["items"]
        borrowed = [r for r in rows if r["borrowed_slot"]]
        self.assertTrue(borrowed, f"第 {n} 笔慢车道应借槽")
        for r in borrowed:
            self.assertEqual(r["lane"], gd.SLOW)
            self.assertEqual(r["end_ms"] - r["start_ms"],
                             gd.hold_ms(gd.SLOW, 0) if r["count"] == 1 else
                             r["end_ms"] - r["start_ms"])

    def test_stable_prefix_when_appending(self):
        base = [ev(i * 1000, 1001, 1) for i in range(5)]
        more = base + [ev(9000, 1004, 30000, 1, uid="u9", user="乙", ch="full")]
        p1 = gd.plan_session_display(items_of(base), one_seg())
        p2 = gd.plan_session_display(items_of(more), one_seg())
        key = lambda r: (r["key"], r["start_ms"], r["end_ms"], r["slot"])
        old = {key(r) for r in p1["segments"][0]["items"]}
        new = {key(r) for r in p2["segments"][0]["items"]}
        self.assertTrue(old.issubset(new), "追加事件不得改变已分配项的起止与槽位")


class TestHold(unittest.TestCase):
    """四档快车道 / 三档慢车道。"""

    def test_fast_ladder(self):
        self.assertEqual(gd.hold_ms(gd.FAST, 3), 1500)
        self.assertEqual(gd.hold_ms(gd.FAST, 6), 1000)
        self.assertEqual(gd.hold_ms(gd.FAST, 12), 800)
        self.assertEqual(gd.hold_ms(gd.FAST, 13), 600)

    def test_slow_ladder(self):
        self.assertEqual(gd.hold_ms(gd.SLOW, 5), 5000)
        self.assertEqual(gd.hold_ms(gd.SLOW, 12), 3000)
        self.assertEqual(gd.hold_ms(gd.SLOW, 13), 2500)


class TestPortraitLayout(unittest.TestCase):
    """portrait-v1：只竖屏生效；行距贴合字号，总高受 2/5 硬上限约束。"""

    def test_1080x1920(self):
        lay = subtitle.gift_layout_portrait(subtitle.Canvas(1080, 1920), {})
        self.assertAlmostEqual(lay["scale"], 1.0)
        self.assertEqual(lay["slow_font"], 38)
        self.assertEqual(lay["fast_font"], 30)
        self.assertEqual(lay["row_height"], 38)
        # 行距必须贴合**实际墨迹**：实测 38 号汉字墨迹约 25px，行高 38 → 净空隙约 13px。
        # 行高超过字号时一定有空隙浪费（用户反馈的"间隔太宽"），这里双向夹住。
        self.assertLessEqual(lay["row_height"], lay["slow_font"] + 2)
        self.assertGreaterEqual(lay["row_height"], lay["slow_font"] * 0.8)
        self.assertEqual(lay["slot_y"][:4], [60, 98, 136, 174])
        self.assertEqual(lay["slots"], 14, "同屏应能放 8 行以上（7~8 行是常态需求）")
        self.assertEqual(lay["area_bottom"], 592)

    def test_area_never_exceeds_two_fifths(self):
        """多行叠加最多占画面 2/5 —— 任何画幅、任何字号都要守住。"""
        for (w, h) in ((1080, 1920), (1088, 1920), (720, 1280), (1080, 1440), (1440, 2560)):
            lay = subtitle.gift_layout_portrait(subtitle.Canvas(w, h), {})
            self.assertLessEqual(lay["area_bottom"], lay["max_area_px"],
                                 f"{w}x{h} 礼物区越界: {lay['area_bottom']} > {lay['max_area_px']}")
            self.assertLessEqual(lay["area_bottom"] / h, 0.4001, f"{w}x{h} 超过 2/5")

    def test_slots_clipped_by_area_budget(self):
        """想要 100 行也只能拿到"2/5 预算"允许的行数，绝不顶到弹幕区。"""
        cfg = {"subtitle": {"gift_display": {"slots_total": 100}}}
        lay = gd.layout_of(gd.config(cfg))
        self.assertLess(lay["slots"], 100)
        self.assertLessEqual(lay["area_bottom"], lay["max_area_px"])

    def test_1088x1920(self):
        lay = subtitle.gift_layout_portrait(subtitle.Canvas(1088, 1920), {})
        self.assertAlmostEqual(lay["scale"], 1.0, places=3)
        self.assertEqual(lay["area_bottom"], 592)

    def test_720x1280(self):
        lay = subtitle.gift_layout_portrait(subtitle.Canvas(720, 1280), {})
        self.assertAlmostEqual(lay["scale"], 2 / 3, places=4)
        self.assertEqual(lay["slow_font"], 25)
        self.assertEqual(lay["fast_font"], 20)
        self.assertEqual(lay["area_bottom"], 395)

    def test_landscape_falls_back(self):
        self.assertIsNone(subtitle.gift_layout_portrait(subtitle.Canvas(1920, 1080), {}))
        self.assertIsNone(subtitle.gift_layout_portrait(subtitle.Canvas(1080, 1080), {}))

    def test_disabled_returns_no_plan(self):
        cfg = {"danmaku": {"style": "queue"},
               "subtitle": {"gift_display": {"enabled": False}}}
        self.assertFalse(gd.enabled(cfg))


class TestCompactStacking(unittest.TestCase):
    """槽位必须连续堆叠：中间留空就是"行距太宽"的元凶。"""

    SEG = [{"idx": 0, "start_ms": 0, "end_ms": 120000}]

    def _ev(self, ts, dia, cnt=1, ch="light"):
        return {"ts": ts, "type": "gift", "gift": f"礼物#{900 + int(dia)}",
                "diamond": dia, "count": cnt, "uid": "", "user": "", "channel": ch}

    def test_one_slow_one_fast_are_adjacent(self):
        items = gd.build_display_items([self._ev(1000, 30000), self._ev(1200, 1)], None, None)
        plan = gd.plan_session_display(items, self.SEG, None)
        slots = sorted(it["slot"] for it in plan["segments"][0]["items"])
        self.assertEqual(slots, [0, 1], "慢+快必须紧邻，不能空出偏好槽")

    def test_eight_rows_on_screen_without_hole(self):
        evs = [self._ev(1000, 30000)] + [self._ev(1001, 1) for _ in range(7)]
        items = gd.build_display_items(evs, None, None)
        plan = gd.plan_session_display(items, self.SEG, None)
        live = [it for it in plan["segments"][0]["items"] if it["start_ms"] < 2000]
        slots = sorted(it["slot"] for it in live)
        self.assertEqual(len(live), 8, "8 笔礼物应同时显示 8 行")
        self.assertEqual(slots, list(range(8)), "8 行必须占满 0..7，不允许空洞")

    def test_slow_item_stays_on_top(self):
        items = gd.build_display_items([self._ev(1000, 30000), self._ev(1200, 1)], None, None)
        plan = gd.plan_session_display(items, self.SEG, None)
        slow = [it for it in plan["segments"][0]["items"] if it["lane"] == "slow"]
        self.assertEqual(slow[0]["slot"], 0, "高价值礼物永远贴顶")

    def test_fast_starts_at_top_when_no_slow(self):
        items = gd.build_display_items([self._ev(1000, 1)], None, None)
        plan = gd.plan_session_display(items, self.SEG, None)
        self.assertEqual(plan["segments"][0]["items"][0]["slot"], 0)


class TestTextFit(unittest.TestCase):
    """超宽时先缩字号，再截昵称；礼物名/份数/金额不得截断。"""

    def test_shrink_then_truncate_nickname(self):
        text, fs = subtitle._fit_gift_text("很长的昵称很长很长的观众甲 送出 小心心 ×999（999 抖币）",
                                           44, 36, 300)
        self.assertEqual(fs, 36)
        self.assertIn("…", text)
        self.assertIn("小心心", text)
        self.assertIn("×999", text)
        self.assertIn("999 抖币", text)

    def test_short_text_untouched(self):
        t, fs = subtitle._fit_gift_text("小心心 ×1", 44, 36, 5000)
        self.assertEqual((t, fs), ("小心心 ×1", 44))


class TestAssRendering(unittest.TestCase):
    """ASS 输出：GiftSlow/GiftFast 样式、\\an7 定位、礼物行数与计划一致。"""

    def setUp(self):
        self._orig = subtitle.Canvas.for_video
        subtitle.Canvas.for_video = classmethod(lambda cls, p: cls(1080, 1920))
        self.tmp = tempfile.TemporaryDirectory()

    def tearDown(self):
        subtitle.Canvas.for_video = self._orig
        self.tmp.cleanup()

    def _render(self, events, cfg=None):
        cfg = cfg or {"danmaku": {"font_size": 44, "style": "queue"},
                      "subtitle": {"gift_display": {"enabled": True}}}
        items = items_of(events, cfg)
        plan = gd.plan_session_display(items, one_seg(), cfg)
        rows = plan["segments"][0]["items"]
        out = os.path.join(self.tmp.name, "主播A-1-000.ass")
        subtitle.generate_ass_for_segment(out, events, ORIGIN, 0.0, cfg,
                                          end_ms=ORIGIN + 60000, video_path="x.flv",
                                          gift_items=rows, session_start_ms=ORIGIN)
        with open(out, encoding="utf-8") as f:
            return f.read(), rows

    def test_styles_and_position(self):
        txt, rows = self._render([ev(1000, 1003, 99), ev(2000, 1001, 1)])
        self.assertIn("Style: GiftSlow", txt)
        self.assertIn("Style: GiftFast", txt)
        gift_lines = [l for l in txt.split("\n") if l.startswith("Dialogue: 2,")]
        self.assertEqual(len(gift_lines), len(rows))
        for l in gift_lines:
            self.assertIn("\\an7\\pos(48,", l)
            self.assertIn(l.split(",")[3], ("GiftSlow", "GiftFast"))

    def test_gift_not_drawn_twice(self):
        """走计划渲染时，队列路径不得再画礼物（否则同一笔出现两次）。"""
        txt, _ = self._render([ev(1000, 1003, 99)])
        self.assertEqual(sum(1 for l in txt.split("\n")
                             if l.startswith("Dialogue:") and ";Gift," in l), 0)

    def test_combo_produces_multiple_segments(self):
        evs = [ev(0, 1001, 1, 1, uid="u1", user="甲", ch="full"),
               ev(1000, 1001, 1, 5, uid="u1", user="甲", ch="full"),
               ev(2000, 1001, 1, 9, uid="u1", user="甲", ch="full")]
        txt, rows = self._render(evs)
        self.assertEqual(len(rows), 1)
        self.assertIn("×1", txt)
        self.assertIn("×9", txt)


class TestLiveDisplay(unittest.TestCase):
    """实时预览语义：full 用 combo key 更新，light 每事件唯一 key。"""

    def test_full_uses_combo_key(self):
        d = gd.build_live_display(ev(0, 1001, 1, 5, uid="u1", user="甲", ch="full"),
                                  None, _Dict(), 1)
        self.assertEqual(d["merge_mode"], "full_combo")
        self.assertEqual(d["key"], "full|u1|1001")
        self.assertTrue(d["merged"])
        self.assertEqual(d["total_diamond"], 5)

    def test_light_key_unique_per_event(self):
        d1 = gd.build_live_display(ev(0, 1001, 1), None, _Dict(), 1)
        d2 = gd.build_live_display(ev(10, 1001, 1), None, _Dict(), 2)
        self.assertEqual(d1["merge_mode"], "none")
        self.assertNotEqual(d1["key"], d2["key"])
        self.assertFalse(d1["merged"])
        self.assertEqual(d1["text"], "小心心 ×1")


class TestGoldenFixture(unittest.TestCase):
    """黄金样例：录制端与下游共用的唯一基线。"""

    def test_fixture_matches_current_impl(self):
        import gift_display_fixture as fx
        self.assertEqual(fx.check(), [], "展示计划与黄金样例漂移，确认后跑 --write 重建")


if __name__ == "__main__":
    unittest.main()
