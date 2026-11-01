# -*- coding: utf-8 -*-
"""字幕画布(PlayRes)与播放器兼容性测试。

背景:抖音直播流是竖屏(典型 1088×1920),而字幕版式常量按 1920×1080 横屏
设计。历史实现对任何视频都写 PlayRes 1920×1080,派生三类问题:
  ① 坐标空间宽高比与画面不符 → 不同播放器映射方式不同(libass 系横纵非等比、
     等比适配的实现把弹幕挤到画面中部);
  ② 自动缩字号的宽度阈值用版式宽度 1920 判断,实际可用宽度只有 1920×W/H,
     长弹幕按原字号渲染后左右被切;且 `\\fs` 标签被写在覆盖块外,会被当正文
     显示(该分支从未触发过,一直没暴露);
  ③ 行间上移动画用 `\\t(0,150,\\pos(...))`,而 libass 不支持用 \\t 改 \\pos,
     文字永远停在起始 y —— 每条弹幕都低一行,最新一条贴在画面最底边。

本文件守住:画布按视频真实尺寸生成、横屏 1080p 行为不变、缩字号真正生效、
位置动画用 \\move、事件按时间排序。
"""
import json
import os
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import subtitle as S


# ── 构造带 onMetaData 的 FLV ──────────────────────────────
def _amf_str(s: str) -> bytes:
    b = s.encode()
    return b"\x02" + struct.pack(">H", len(b)) + b


def _amf_num(v: float) -> bytes:
    return b"\x00" + struct.pack(">d", v)


def _amf_key(k: str) -> bytes:
    b = k.encode()
    return struct.pack(">H", len(b)) + b


def _meta_body(width=None, height=None, duration=2301.55):
    """onMetaData 载荷(AMF0 ECMA array)。width/height 为 None 时不写该键。"""
    body = _amf_str("onMetaData") + b"\x08" + struct.pack(">I", 3)
    body += _amf_key("duration") + _amf_num(duration)
    if width is not None:
        body += _amf_key("width") + _amf_num(width)
    if height is not None:
        body += _amf_key("height") + _amf_num(height)
    body += b"\x00\x00\x09"
    return body


def _flv_tag(tag_type: int, ts_ms: int, payload: bytes) -> bytes:
    header = bytes([tag_type]) + struct.pack(">I", len(payload))[1:] + \
        struct.pack(">I", ts_ms & 0xFFFFFF)[1:] + bytes([(ts_ms >> 24) & 0xFF]) + b"\x00\x00\x00"
    return header + payload + struct.pack(">I", 11 + len(payload))


def _make_flv(path: str, width=None, height=None, with_meta=True, meta_body=None):
    """写一个合法 FLV:可选 script tag(onMetaData) + 一个视频 tag。"""
    with open(path, "wb") as f:
        f.write(b"FLV\x01\x05\x00\x00\x00\x09")
        f.write(struct.pack(">I", 0))
        if with_meta:
            payload = meta_body if meta_body is not None else _meta_body(width, height)
            f.write(_flv_tag(18, 0, payload))
        f.write(_flv_tag(9, int(time.time() * 1000), b"\x00" * 8))


def _events(base_ms, items):
    """items: [(offset_ms, 'chat'/'gift', text)] → 事件列表。"""
    out = []
    for off, kind, text in items:
        if kind == "gift":
            out.append({"ts": base_ms + off, "type": "gift", "user": text,
                        "uid": "9", "gift": "礼物#16229", "count": 2, "diamond": 100})
        else:
            out.append({"ts": base_ms + off, "type": "chat", "user": text,
                        "uid": "1", "content": text})
    return out


CFG = {"danmaku": {"style": "queue", "font_size": 44, "queue_lines": 5,
                   "queue_seconds": 8, "capture_member": False, "offset_seconds": 0}}


class TestFlvVideoSize(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.d = self.dir.name
        self.p = os.path.join(self.d, "a.flv")

    def tearDown(self):
        self.dir.cleanup()

    def test_portrait(self):
        _make_flv(self.p, 1088, 1920)
        self.assertEqual(S.flv_video_size(self.p), (1088, 1920))

    def test_landscape(self):
        _make_flv(self.p, 1920, 1080)
        self.assertEqual(S.flv_video_size(self.p), (1920, 1080))

    def test_missing_keys(self):
        _make_flv(self.p, None, None)
        self.assertIsNone(S.flv_video_size(self.p))

    def test_only_one_key(self):
        _make_flv(self.p, 1088, None)
        self.assertIsNone(S.flv_video_size(self.p))

    def test_no_script_tag(self):
        _make_flv(self.p, 1088, 1920, with_meta=False)
        self.assertIsNone(S.flv_video_size(self.p))

    def test_not_flv(self):
        with open(self.p, "wb") as f:
            f.write(b"notflv!" * 4)
        self.assertIsNone(S.flv_video_size(self.p))

    def test_absurd_value_rejected(self):
        _make_flv(self.p, 4, 99999)
        self.assertIsNone(S.flv_video_size(self.p))

    def test_missing_file(self):
        self.assertIsNone(S.flv_video_size(os.path.join(self.d, "nope.flv")))

    def test_truncated_body_no_crash(self):
        _make_flv(self.p, 1088, 1920, meta_body=b"\x02\x00\x0aonMetaData\x08")
        self.assertIsNone(S.flv_video_size(self.p))

    def test_extra_keys_skipped(self):
        """真实 onMetaData 里 width/height 夹在其它键之间,必须能跳过。"""
        body = _amf_str("onMetaData") + b"\x08" + struct.pack(">I", 6)
        body += _amf_key("duration") + _amf_num(120.5)
        body += _amf_key("width") + _amf_num(1088)
        body += _amf_key("height") + _amf_num(1920)
        body += _amf_key("framerate") + _amf_num(22.0)
        body += _amf_key("videocodecid") + _amf_num(7)
        body += _amf_key("hasVideo") + b"\x01\x01"          # boolean
        body += _amf_key("comment") + _amf_str("by ffmpeg")  # string
        body += b"\x00\x00\x09"
        _make_flv(self.p, meta_body=body)
        self.assertEqual(S.flv_video_size(self.p), (1088, 1920))


class TestCanvas(unittest.TestCase):
    def _canvas(self, w, h):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = os.path.join(d.name, "v.flv")
        _make_flv(p, w, h)
        return S.Canvas.for_video(p)

    def test_fallback_without_video(self):
        self.assertIsNone(S.flv_video_size("/nonexistent/nope.flv"))
        c = S.Canvas.for_video(None)
        self.assertEqual((c.w, c.h), (S.DESIGN_W, S.DESIGN_H))
        self.assertTrue(c.is_design)
        # 版式基准画布是恒等换算
        self.assertEqual((c.x(960), c.y(1020), c.size(44)), (960, 1020, 44))

    def test_portrait_mapping(self):
        c = self._canvas(1088, 1920)
        self.assertEqual((c.w, c.h), (1088, 1920))
        self.assertFalse(c.is_design)
        self.assertEqual(c.x(960), 544)          # 横向:1088/1920
        self.assertEqual(c.x(1920), 1088)        # 版式满宽 = 画布满宽
        self.assertEqual(c.y(1080), 1920)        # 版式满高 = 画布满高
        self.assertEqual(c.y(1020), 1813)        # 队列底行
        self.assertEqual(c.size(44), 78)         # 字号跟随高度

    def test_landscape_is_identity(self):
        """横屏 1080p 视频下换算恒等 —— 历史输出不受影响。"""
        c = self._canvas(1920, 1080)
        self.assertTrue(c.is_design)
        for v in (60, 804, 1020, 1074, 1920):
            self.assertEqual(c.y(v), v)
            self.assertEqual(c.x(v), v)
        self.assertEqual(c.size(44), 44)


class TestGeneratedAss(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.d = self.dir.name
        self.base = int(time.time() * 1000) // 1000 * 1000
        self.evs = _events(self.base, [
            (1000, "chat", "观众甲"),
            (1800, "chat", "观众乙"),
            (2000, "gift", "大哥B"),
            (2600, "chat", "观众丙"),
        ])

    def tearDown(self):
        self.dir.cleanup()

    def _gen(self, w, h, events=None, name="x", cfg=None):
        self.video = os.path.join(self.d, name + ".flv")
        _make_flv(self.video, w, h)
        out = os.path.join(self.d, name + ".ass")
        S.generate_ass_for_segment(out, events if events is not None else self.evs,
                                   self.base, 0.0, cfg or CFG, video_path=self.video)
        return self._read(out)

    def _read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def _dialogue(self, content):
        return [ln for ln in content.splitlines() if ln.startswith("Dialogue")]

    def test_portrait_header_and_positions(self):
        c = self._gen(1088, 1920)
        self.assertIn("PlayResX: 1088", c)
        self.assertIn("PlayResY: 1920", c)
        self.assertIn("Style: Danmaku,PingFang SC,78,", c)     # 44 × 1920/1080
        self.assertIn("\\pos(544,", c)                          # 居中 x
        self.assertIn("\\move(544,", c)                         # 上移动画
        self.assertNotIn("\\pos(960,", c)

    def test_landscape_positions_unchanged(self):
        """横屏 1080p:坐标与字号与历史版本一致。"""
        c = self._gen(1920, 1080)
        self.assertIn("PlayResX: 1920", c)
        self.assertIn("PlayResY: 1080", c)
        self.assertIn("Style: Danmaku,PingFang SC,44,", c)
        self.assertIn("\\pos(960,", c)
        self.assertIn("\\move(960,1074,960,1020,0,150)", c)

    def test_no_metadata_falls_back(self):
        self.video = os.path.join(self.d, "nometa.flv")
        _make_flv(self.video, None, None)
        out = os.path.join(self.d, "nometa.ass")
        S.generate_ass_for_segment(out, self.evs, self.base, 0.0, CFG,
                                   video_path=self.video)
        c = self._read(out)
        self.assertIn("PlayResX: 1920", c)
        self.assertIn("\\pos(960,", c)

    def test_sibling_video_detected(self):
        """不传 video_path 时,由 .ass 同目录同名视频推断画布。"""
        self.video = os.path.join(self.d, "sib.flv")
        _make_flv(self.video, 1088, 1920)
        out = os.path.join(self.d, "sib.ass")
        S.generate_ass_for_segment(out, self.evs, self.base, 0.0, CFG)
        c = self._read(out)
        self.assertIn("PlayResX: 1088", c)
        self.assertEqual(S._sibling_video(out), self.video)

    def test_sibling_video_absent(self):
        out = os.path.join(self.d, "lonely.ass")
        self.assertIsNone(S._sibling_video(out))
        S.generate_ass_for_segment(out, self.evs, self.base, 0.0, CFG)
        self.assertIn("PlayResX: 1920", self._read(out))

    # ── 长文本自动缩字号 ──
    def test_long_line_shrinks_to_fit_portrait(self):
        # 20 个全角字:超过竖屏可用宽度,但缩完不触底,应正好放得下
        evs = [{"ts": self.base + 1000, "type": "chat", "user": "", "uid": "1",
                "content": "很长的一条弹幕内容啊真的挺长的" + "补足二十字长度"}]
        c = self._gen(1088, 1920, events=evs, name="long")
        line = [x for x in self._dialogue(c) if "\\fs" in x]
        self.assertTrue(line, "长弹幕应触发缩字号")
        fs = int(line[0].split("\\fs")[1].split("}")[0])
        canvas = S.Canvas(1088, 1920)
        self.assertLess(fs, canvas.size(44))
        self.assertGreater(fs, canvas.size(S.FONT_SIZE_FLOOR))
        # 缩完后估算宽度不超过画布可用宽度
        text = line[0].split("}", 1)[1]
        self.assertLessEqual(S._text_em(text) * fs,
                             canvas.w - canvas.x(S.TEXT_SIDE_MARGIN))

    def test_short_line_not_shrunk(self):
        c = self._gen(1088, 1920, events=_events(self.base, [(1000, "chat", "大家好")]),
                      name="short")
        self.assertNotIn("\\fs", c)

    def test_design_canvas_shrink_matches_legacy(self):
        """版式基准画布下,纯 CJK 长文本的缩字号结果与历史公式一致。

        用空 user 构造纯 CJK 正文,使 _text_em 的估算与历史 len() 完全等价。
        """
        evs = [{"ts": self.base + 1000, "type": "chat", "user": "", "uid": "1",
                "content": "中" * 60}]
        c = self._gen(1920, 1080, events=evs, name="long2")
        line = [x for x in self._dialogue(c) if "\\fs" in x][0]
        fs = int(line.split("\\fs")[1].split("}")[0])
        self.assertEqual(fs, max(20, int(44 * (1920 - 80) / (60 * 44))))
        self.assertEqual(fs, 30)

    def test_fs_tag_inside_override_block(self):
        """\\fs 必须在 {} 内 —— 写在块外会被播放器当正文原样显示。"""
        evs = [{"ts": self.base + 1000, "type": "chat", "user": "",
                "uid": "1", "content": "超长弹幕" * 12}]
        c = self._gen(1088, 1920, events=evs, name="long3")
        seen = 0
        for line in self._dialogue(c):
            body = line.split(",", 9)[9]      # 第 10 个字段 = Text
            if "\\fs" in body:
                seen += 1
                head, _, tail = body.partition("}")
                self.assertIn("\\fs", head, "\\fs 必须在覆盖块内")
                self.assertNotIn("\\fs", tail, "覆盖块外不得出现 \\fs")
                self.assertTrue(head.startswith("{\\an2\\pos(") or
                                head.startswith("{\\an2\\move("))
        self.assertGreater(seen, 0)

    # ── 位置动画标签 ──
    def test_slide_uses_move_not_t_pos(self):
        c = self._gen(1088, 1920)
        self.assertIn("\\move(", c)
        self.assertNotIn("\\t(0,150,", c)

    def test_move_endpoint_is_design_row(self):
        """\\move 的终点必须是设计行坐标(而非停在起始的屏外位置)。"""
        c = self._gen(1088, 1920)
        self.assertIn("\\move(544,1909,544,1813,0,150)", c)   # 屏外底 → 底行
        self.assertNotIn("\\move(544,1909,544,1909", c)
        # 礼物横幅是静止段,用 \pos 且落在版式顶部 60 处
        self.assertIn(r"{\an2\pos(544,107)}", c)

    # ── 事件排序 ──
    def test_dialogues_sorted_by_start(self):
        c = self._gen(1088, 1920)
        starts = [S._ass_secs(x.split(",", 2)[1]) for x in self._dialogue(c)]
        self.assertEqual(starts, sorted(starts))
        # 礼物嵌入弹幕时间轴,不再整块排在前面
        first_gift = next(i for i, x in enumerate(self._dialogue(c))
                          if x.startswith("Dialogue: 1,"))
        self.assertGreater(first_gift, 0)

    def test_sort_keeps_all_rows(self):
        c = self._gen(1088, 1920)
        self.assertEqual(len(self._dialogue(c)),
                         len(self._dialogue(self._gen(1088, 1920, name="y"))))

    def test_sort_helper_keeps_bad_time_last(self):
        out = S._sort_dialogues(["Dialogue: 0,0:00:05.00,x", "Dialogue: 0,坏,x",
                                 "Dialogue: 0,0:00:01.00,x"])
        self.assertTrue(out[-1].endswith("坏,x"))
        self.assertTrue(out[0].endswith("0:00:01.00,x"))


class TestFitFont(unittest.TestCase):
    def setUp(self):
        self.c = S.Canvas(1088, 1920)

    def test_short_text_untouched(self):
        fs, shrunk = S._fit_font("短", 44, self.c)
        self.assertEqual(fs, 78)
        self.assertFalse(shrunk)

    def test_floor_applied(self):
        fs, shrunk = S._fit_font("中" * 500, 44, self.c)
        self.assertTrue(shrunk)
        self.assertEqual(fs, self.c.size(S.FONT_SIZE_FLOOR))

    def test_width_uses_canvas_not_design(self):
        """同一段文本在竖屏画布下会缩、在横屏画布下不缩 —— 旧实现两者都不缩。"""
        text = "观众甲: " + "内容" * 10
        portrait, _ = S._fit_font(text, 44, self.c)
        landscape, shrunk_l = S._fit_font(text, 44, S.Canvas(1920, 1080))
        self.assertLess(portrait, 78)
        self.assertEqual(landscape, 44)
        self.assertFalse(shrunk_l)

    def test_text_em_weights(self):
        self.assertAlmostEqual(S._text_em("中"), 1.0)
        self.assertAlmostEqual(S._text_em("ab"), 1.1)


class TestSessionCanvasIntegration(unittest.TestCase):
    """分片 → 画布:每片按自身视频取尺寸(同场混排也各归各的)。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.d = self.dir.name
        self.stem = "主播-20260822-140000"
        self.pattern = os.path.join(self.d, self.stem + "-%03d.flv")
        t0 = int(time.time() * 1000)
        self.t0 = t0
        # 片 0 竖屏、片 1 横屏:两片必须得到各自的画布。
        # 首 tag 用 epoch 毫秒(截断 32 位),让 anchor_segments 能解回绕。
        for i, (w, h) in enumerate(((1088, 1920), (1920, 1080))):
            p = os.path.join(self.d, f"{self.stem}-{i:03d}.flv")
            _make_flv(p, w, h)
            with open(p, "ab") as f:
                f.write(_flv_tag(9, (t0 + i * 600_000) & 0xFFFFFFFF, b"\x00" * 8))
        jsonl = os.path.join(self.d, ".meta", self.stem + ".danmaku.jsonl")
        os.makedirs(os.path.dirname(jsonl), exist_ok=True)
        with open(jsonl, "w", encoding="utf-8") as f:
            for i in (0, 1):
                f.write(json.dumps({"ts": t0 + i * 600_000 + 1000, "type": "chat",
                                    "user": "观众甲", "uid": "1",
                                    "content": f"第{i}片"}, ensure_ascii=False) + "\n")

    def tearDown(self):
        self.dir.cleanup()

    def _read(self, path):
        with open(path, encoding="utf-8") as f:
            return f.read()

    def test_each_segment_gets_own_canvas(self):
        info = S.generate_session(self.pattern, CFG)
        self.assertEqual(len(info), 2)
        c0 = self._read(S.ass_path_for_segment(info[0]["file"]))
        c1 = self._read(S.ass_path_for_segment(info[1]["file"]))
        self.assertIn("PlayResX: 1088", c0)
        self.assertIn("PlayResY: 1920", c0)
        self.assertIn("Style: Danmaku,PingFang SC,78,", c0)
        self.assertIn("PlayResX: 1920", c1)
        self.assertIn("PlayResY: 1080", c1)
        self.assertIn("Style: Danmaku,PingFang SC,44,", c1)


if __name__ == "__main__":
    unittest.main()
