# -*- coding: utf-8 -*-
"""弹幕字幕对齐与生成本元测试。"""
import json
import os
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import subtitle
from danmaku import jsonl_for


def _flv_tag(tag_type: int, ts_ms: int, data_len: int = 8) -> bytes:
    """构造一个 FLV tag(header + body + prevSize 占位)。"""
    lower = ts_ms & 0xFFFFFF
    ext = (ts_ms >> 24) & 0xFF
    header = bytes([tag_type]) + struct.pack(">I", data_len)[1:] + \
        struct.pack(">I", lower)[1:] + bytes([ext]) + b"\x00\x00\x00"
    return header + b"\x00" * data_len + struct.pack(">I", 11 + data_len)


def _make_flv(path: str, tags):
    """tags: [(tag_type, ts_ms)] → 写一个合法 FLV 文件。"""
    with open(path, "wb") as f:
        f.write(b"FLV\x01\x05\x00\x00\x00\x09")      # header 9B
        f.write(struct.pack(">I", 0))                  # PreviousTagSize0
        for t, ts in tags:
            f.write(_flv_tag(t, ts))


class TestFlvParse(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, "a.flv")

    def tearDown(self):
        self.dir.cleanup()

    def test_first_av_ts(self):
        # metadata(tag18, ts=100) 在前,首个视频 tag 在后;时间戳为 epoch ms
        # _flv_tag 写入时自动截断为 32 位(与 ffmpeg 实际行为一致)
        now = int(time.time() * 1000)
        _make_flv(self.path, [(18, 100), (9, now), (9, now + 2000)])
        # 原始解析:32 位截断值
        self.assertEqual(subtitle.flv_first_av_ts(self.path), now & 0xFFFFFFFF)
        self.assertEqual(subtitle.flv_last_ts(self.path), (now + 2000) & 0xFFFFFFFF)
        # 绝对值:以 birthtime 解回绕,精确还原 epoch 毫秒
        first = subtitle.flv_first_av_ts_abs(self.path)
        last = subtitle.flv_last_ts_abs(self.path)
        self.assertAlmostEqual(first, now, delta=100)
        self.assertAlmostEqual(last, now + 2000, delta=100)
        self.assertTrue(subtitle.is_epoch_ms(first))

    def test_relative_ts_not_epoch(self):
        _make_flv(self.path, [(9, 0), (9, 5000)])
        self.assertEqual(subtitle.flv_first_av_ts(self.path), 0)
        # 相对时间戳模式(copyts 未生效)→ 绝对解析返回 None
        self.assertIsNone(subtitle.flv_first_av_ts_abs(self.path))
        self.assertFalse(subtitle.is_epoch_ms(subtitle.flv_first_av_ts(self.path)))

    def test_ext_timestamp(self):
        # >16.7 天的毫秒需要扩展位;模拟一个跨扩展位的值
        big = 0x01_234567  # 19088743 ms
        _make_flv(self.path, [(9, big)])
        self.assertEqual(subtitle.flv_first_av_ts(self.path), big)

    def test_bad_file(self):
        with open(self.path, "wb") as f:
            f.write(b"notflv!")
        self.assertIsNone(subtitle.flv_first_av_ts(self.path))
        self.assertIsNone(subtitle.flv_last_ts(self.path))

    def test_truncated_tail_rejected(self):
        """末尾不是完整 tag(ffmpeg 仍在写)→ 返回 None,不产出荒谬时间戳。"""
        now = int(time.time() * 1000)
        _make_flv(self.path, [(9, now), (9, now + 2000)])
        with open(self.path, "ab") as f:
            f.write(b"\x09\x00\x00")  # 半个 tag 头:末尾 4 字节不是合法 prevSize
        self.assertIsNone(subtitle.flv_last_ts(self.path))
        self.assertIsNone(subtitle.flv_last_ts_abs(self.path))

    def test_inconsistent_tag_size_rejected(self):
        """tag 头自洽校验:prevSize 与头中 data_size 不符 → 判为定位失败。"""
        _make_flv(self.path, [(9, int(time.time() * 1000))])
        with open(self.path, "rb") as f:
            data = bytearray(f.read())
        struct.pack_into(">I", data, len(data) - 4, 11 + 99)  # 与 data_len=8 矛盾
        with open(self.path, "wb") as f:
            f.write(bytes(data))
        self.assertIsNone(subtitle.flv_last_ts(self.path))

    def test_implausible_unwrap_rejected(self):
        """tag 头合法但时间戳被解绕到 ±24.8 天边界 → 判为不可信(防 500+ 小时告警)。"""
        _make_flv(self.path, [(9, int(time.time() * 1000))])
        birth = subtitle._birthtime_ms(self.path)
        bogus = birth - 22 * 86400_000  # 22 天前:正是解绕边界附近的垃圾值
        _make_flv(self.path, [(9, bogus)])
        self.assertIsNone(subtitle.flv_last_ts_abs(self.path))

    def test_normal_last_ts_still_works(self):
        """正常时间戳不能被误杀:分片内的 last 应正常解出。"""
        now = int(time.time() * 1000)
        _make_flv(self.path, [(9, now), (9, now + 40_000)])
        self.assertAlmostEqual(subtitle.flv_last_ts_abs(self.path), now + 40_000, delta=500)


class TestJsonlPath(unittest.TestCase):
    def test_pattern(self):
        self.assertEqual(
            jsonl_for("/r/主播A/主播A-20260822-140000-%03d.flv"),
            "/r/主播A/.meta/主播A-20260822-140000.danmaku.jsonl")

    def test_single(self):
        self.assertEqual(
            jsonl_for("/r/主播A/主播A-20260822-140000.flv"),
            "/r/主播A/.meta/主播A-20260822-140000.danmaku.jsonl")

    def test_mp4(self):
        self.assertTrue(jsonl_for("/r/a-%03d.mp4").endswith(".danmaku.jsonl"))


class TestSegmentsAndAss(unittest.TestCase):
    """分片发现 → 锚定 → ASS 生成 → 偏移重生成 全链路。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.d = self.dir.name
        self.stem = "主播-20260822-140000"
        self.pattern = os.path.join(self.d, self.stem + "-%03d.flv")
        self.cfg = {"danmaku": {"enabled": True, "offset_seconds": 0,
                                "font_size": 44, "capture_member": False}}
        # 三个分片,时间戳是绝对毫秒(copyts 生效形态,写入时截断 32 位),
        # 每片 10 分钟;必须用当前墙钟(解回绕以 birthtime 为基准)
        t0 = int(time.time() * 1000)
        self.t0 = t0
        for i in range(3):
            _make_flv(os.path.join(self.d, f"{self.stem}-{i:03d}.flv"),
                      [(9, t0 + i * 600_000), (9, t0 + i * 600_000 + 599_000)])
        # jsonl:每片区间内 2 条弹幕 + 1 条礼物
        events = []
        for i in range(3):
            for j in range(2):
                events.append({"ts": t0 + i * 600_000 + 1000 + j * 2000,
                               "type": "chat", "user": f"用户{j}", "uid": "1",
                               "content": f"弹幕{i}-{j}"})
            events.append({"ts": t0 + i * 600_000 + 3000, "type": "gift",
                           "user": "老板", "uid": "2", "gift": "嘉年华",
                           "count": 1, "diamond": 3000})
        # 一条早于视频起点的弹幕(应被丢弃)
        events.append({"ts": t0 - 5000, "type": "chat", "user": "x", "uid": "3",
                       "content": "录制开始前"})
        self.jsonl = os.path.join(self.d, ".meta", self.stem + ".danmaku.jsonl")
        os.makedirs(os.path.dirname(self.jsonl), exist_ok=True)
        with open(self.jsonl, "w", encoding="utf-8") as f:
            for e in events:
                f.write(json.dumps(e, ensure_ascii=False) + "\n")

    def tearDown(self):
        self.dir.cleanup()

    def test_find_segments(self):
        segs = subtitle.find_segments(self.pattern)
        self.assertEqual([s[0] for s in segs], ["000", "001", "002"])
        single = subtitle.find_segments(os.path.join(self.d, self.stem + "-000.flv"))
        self.assertEqual(single[0][0], None)  # 单文件模式(该文件真实存在)

    def test_generate_and_offsets(self):
        info = subtitle.generate_session(self.pattern, self.cfg)
        self.assertEqual(len(info), 3)
        for seg in info:
            ass = subtitle.ass_path_for_segment(seg["file"])
            self.assertTrue(os.path.exists(ass))
            with open(ass, encoding="utf-8") as f:
                content = f.read()
            self.assertIn("[Script Info]", content)
            self.assertIn("弹幕", content)  # 文本被转义写入
        # 分片 0 的字幕含第 0 区间弹幕、不含后续区间
        with open(subtitle.ass_path_for_segment(info[0]["file"]), encoding="utf-8") as f:
            c0 = f.read()
        self.assertIn("弹幕0-0", c0)
        self.assertNotIn("弹幕1-0", c0)
        self.assertNotIn("录制开始前", c0)
        # 礼物横幅与底部队列弹幕都有
        self.assertIn("嘉年华", c0)
        self.assertIn("\\an2", c0)        # 队列样式底部对齐
        self.assertIn("\\t(0,150,", c0)   # 行间上移动画

        # 偏移重生成:分片 1 加 -2.5s,全局加 1s
        align = subtitle.load_align(self.pattern)
        align["global_offset"] = 1.0
        align.setdefault("per_segment", {})["001"] = -2.5
        subtitle.generate_session(self.pattern, self.cfg, align)
        with open(subtitle.ass_path_for_segment(
                os.path.join(self.d, self.stem + "-001.flv")), encoding="utf-8") as f:
            c1 = f.read()
        # 分片 1 事件 t = (ts - anchor)/1000 + (1 + -2.5) = 1 + -1.5 = -0.5 → 首条被钳到 0
        # 分片 0 不受 per_segment 影响
        with open(subtitle.ass_path_for_segment(
                os.path.join(self.d, self.stem + "-000.flv")), encoding="utf-8") as f:
            c0b = f.read()
        self.assertIn("0:00:02.00", c0b)  # 全局 +1:弹幕0-0 从 1s 延后到 2s

    def test_effective_offset_fallback_config(self):
        align = {}  # 无人工偏移 → 用配置默认
        self.assertEqual(subtitle.effective_offset(align, self.cfg, "000"), 0.0)
        cfg2 = {"danmaku": {"offset_seconds": -1.5}}
        self.assertEqual(subtitle.effective_offset(align, cfg2, "000"), -1.5)

    # ── 礼物独立偏移(与弹幕分开的三层:配置 / 本场人工 / 单分片) ──
    def test_gift_offset_default_falls_back_to_danmaku(self):
        """默认礼物额外偏移为 0 → 礼物与弹幕时刻完全一致(向后兼容)。"""
        align = {}
        self.assertEqual(subtitle.gift_extra_offset(align, self.cfg, "000"), 0.0)
        self.assertEqual(subtitle.effective_gift_offset(align, self.cfg, "000"),
                         subtitle.effective_offset(align, self.cfg, "000"))
        # 老配置没有 gift_offset_seconds 键也不能炸
        self.assertEqual(subtitle.gift_extra_offset(align, {"danmaku": {}}, "000"), 0.0)

    def test_gift_offset_layers(self):
        """优先级:本场人工 > 配置默认;单分片在全局之上再叠加。"""
        cfg = {"danmaku": {"offset_seconds": 1, "gift_offset_seconds": -2}}
        align = {}
        # 配置层:弹幕 +1,礼物额外 -2(总 -1)
        self.assertEqual(subtitle.effective_offset(align, cfg, "000"), 1.0)
        self.assertEqual(subtitle.gift_extra_offset(align, cfg, "000"), -2.0)
        self.assertEqual(subtitle.effective_gift_offset(align, cfg, "000"), -1.0)
        # 人工本场覆盖配置的礼物值,弹幕值不受影响
        align = {"gift_global_offset": 0.5, "gift_per_segment": {"001": -0.25}}
        self.assertEqual(subtitle.effective_offset(align, cfg, "000"), 1.0)
        self.assertEqual(subtitle.gift_extra_offset(align, cfg, "000"), 0.5)
        self.assertEqual(subtitle.gift_extra_offset(align, cfg, "001"), 0.25)
        self.assertEqual(subtitle.effective_gift_offset(align, cfg, "001"), 1.25)

    def test_gift_extra_shifts_only_gift_lines(self):
        """生成 ASS 时:礼物横幅整体位移,弹幕时刻一格不动。"""
        evs = [{"ts": self.t0 + 1000, "type": "chat", "user": "A", "uid": "1",
                "content": "嗨"},
               {"ts": self.t0 + 1000, "type": "gift", "user": "B", "uid": "2",
                "gift": "嘉年华", "count": 1, "diamond": 3000}]
        out = os.path.join(self.d, "gift-shift.ass")
        subtitle.generate_ass_for_segment(out, evs, self.t0, 0.0, self.cfg,
                                          gift_extra=3.0)
        with open(out, encoding="utf-8") as f:
            lines = f.read().splitlines()
        chat = [x for x in lines if x.startswith("Dialogue: 0,")]
        gift = [x for x in lines if x.startswith("Dialogue: 1,")]
        self.assertTrue(chat, "应有弹幕行")
        self.assertTrue(gift, "应有礼物横幅行")
        # 事件在 anchor+1s:弹幕 1s 起;礼物 +3s → 4s 起
        self.assertEqual(chat[0].split(",")[1], "0:00:01.00")
        self.assertEqual(gift[0].split(",")[1], "0:00:04.00")

    def test_gift_offset_in_session_info_and_align(self):
        """generate_session:info 带礼物偏移,align.json 快照配置值。"""
        cfg = {"danmaku": {"font_size": 44, "gift_offset_seconds": -1.5,
                           "offset_seconds": 0.5}}
        info = subtitle.generate_session(self.pattern, cfg)
        for seg in info:
            self.assertEqual(seg["offset"], 0.5)
            self.assertEqual(seg["gift_extra"], -1.5)
            self.assertEqual(seg["gift_offset"], -1.0)
        align = subtitle.load_align(self.pattern)
        self.assertEqual(align["config_offset"], 0.5)
        self.assertEqual(align["gift_config_offset"], -1.5)

    def test_summarize(self):
        s = subtitle.summarize(self.jsonl)
        # 7 条 chat:6 条分段内 + 1 条录制启动前(汇总统计全场 jsonl 原始明细,
        # 早于视频起点的事件仅在 ASS 生成时被过滤,汇总不滤)
        self.assertEqual(s["chat"], 7)
        self.assertEqual(s["gift"], 3)
        self.assertEqual(s["diamonds"], 9000)
        self.assertEqual(s["gift_top"][0], {"gift": "嘉年华", "count": 3})

    def test_patrol_stall_detection(self):
        # 把最后分片时间戳改成 2 分钟前 → 巡检报告停滞
        stale = int(time.time() * 1000) - 120_000
        _make_flv(os.path.join(self.d, f"{self.stem}-002.flv"),
                  [(9, stale), (9, stale + 100)])
        _, anomalies = subtitle.patrol(self.pattern, self.cfg, last_msg_ts=time.time())
        self.assertTrue(any("停滞" in a for a in anomalies))
        gaps = subtitle.load_align(self.pattern).get("gaps", [])
        self.assertTrue(any(g["type"] == "stall" for g in gaps))

    def test_danmaku_gap_detection(self):
        _, anomalies = subtitle.patrol(self.pattern, self.cfg,
                                       last_msg_ts=time.time() - 120)
        self.assertTrue(any("弹幕中断" in a for a in anomalies))


class TestAssStyles(unittest.TestCase):
    """字幕样式:queue(底部 5 行队列)与 scroll(横向滚动)渲染路径。"""

    def _gen(self, events, style, **kw):
        d = tempfile.TemporaryDirectory().name
        os.makedirs(d, exist_ok=True)
        base = int(time.time() * 1000)
        evs = [{"ts": base + t, "type": "chat", "user": "u", "uid": "1",
                "content": txt} for t, txt in events]
        cfg = {"danmaku": {"style": style, "font_size": 44, **kw}}
        out = os.path.join(d, "x.ass")
        subtitle.generate_ass_for_segment(out, evs, base, 0.0, cfg)
        with open(out, encoding="utf-8") as f:
            return f.read()

    def test_queue_five_lines(self):
        # 6 条弹幕间隔 1s:第 6 条到来时第 1 条被顶出;font44→line_h54,
        # 底行 y=1020,行1 y=804,屏外底部 1074,屏外顶部 6
        c = self._gen([(1000, "弹幕0"), (2000, "弹幕1"), (3000, "弹幕2"),
                       (4000, "弹幕3"), (5000, "弹幕4"), (6000, "弹幕5")], "queue")
        for i in range(6):
            self.assertIn(f"弹幕{i}", c)
        # 最新一条:从屏外底部(1074)滑入底行(1020)
        self.assertIn(r"{\an2\pos(960,1074)\t(0,150,\pos(960,1020))}u: 弹幕5", c)
        # 第 1 条被顶出:最后一段从行 1(804)滑出顶部(6)
        self.assertIn(r"\pos(960,804)\t(0,150,\pos(960,6))", c)
        # 每条弹幕段数 ≤ 6(最多 5 行 + 1 段滑出)
        for i in range(6):
            self.assertLessEqual(c.count(f"弹幕{i}"), 6)

    def test_queue_sparse_hold(self):
        # 稀疏弹幕:单条停留不超过 queue_seconds(8s) → 无第二条时显示 [t, t+8)
        c = self._gen([(1000, "孤独")], "queue")
        self.assertIn("0:00:01.00,0:00:09.00", c)

    def test_scroll_style(self):
        c = self._gen([(1000, "横向"), (2000, "滚动")], "scroll")
        self.assertIn("\\move(", c)
        self.assertNotIn("\\an2", c)


class TestAssText(unittest.TestCase):
    def test_escape(self):
        self.assertNotIn("{", subtitle._esc("a{b}c\\d\ne"))
        self.assertIn("｛", subtitle._esc("a{b}"))

    def test_ass_ts(self):
        self.assertEqual(subtitle._ass_ts(0), "0:00:00.00")
        self.assertEqual(subtitle._ass_ts(3661.5), "1:01:01.50")
        self.assertEqual(subtitle._ass_ts(-5), "0:00:00.00")


if __name__ == "__main__":
    unittest.main()
