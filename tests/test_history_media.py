# -*- coding: utf-8 -*-
"""历史记录「实际分辨率 + 实测码率」的单元测试(临时目录,不碰真实数据)。

背景:历史页原来只有 时长/段数/体积。用户要求「能明确看见码率和分辨率」。
配置里的 recorder.resolution / recorder.bitrate 是**目标值**,而落盘文件才是
**事实** —— 自动降级、源本身低清、流复制(copy)不重编码,三种情况都会让两者
对不上,「看着在录、其实参数不对」正是这套系统最危险的失败模式。

本文件守住:
  ① 单文件 / 分片的尺寸读取与平均码率算法(字节×8÷秒);
  ② 中途降级(末片尺寸变了)必须能被识别并带出 tail,而不是只报首片;
  ③ 文件不在(已删/归档未挂载)时降级为「只报码率」或 None,绝不抛异常;
  ④ 收尾(_history_finish)时写入,存量条目由 webui 惰性补算;
  ⑤ 前端历史行确实渲染这两个胶囊(fmtKbps 口径 + 胶囊顺序)。
"""
import json
import os
import re
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import monitor  # noqa: E402
import webui  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HTML = os.path.join(BASE, "static", "index.html")


# ── 构造带 onMetaData 的 FLV(与 tests/test_subtitle_canvas.py 同一套写法)──
def _amf_str(s: str) -> bytes:
    b = s.encode()
    return b"\x02" + struct.pack(">H", len(b)) + b


def _amf_num(v: float) -> bytes:
    return b"\x00" + struct.pack(">d", v)


def _amf_key(k: str) -> bytes:
    b = k.encode()
    return struct.pack(">H", len(b)) + b


def _flv_tag(tag_type: int, ts_ms: int, payload: bytes) -> bytes:
    header = bytes([tag_type]) + struct.pack(">I", len(payload))[1:] + \
        struct.pack(">I", ts_ms & 0xFFFFFF)[1:] + bytes([(ts_ms >> 24) & 0xFF]) + b"\x00\x00\x00"
    return header + payload + struct.pack(">I", 11 + len(payload))


def _make_flv(path: str, width=None, height=None, spans=None):
    """写一个合法 FLV:script tag(onMetaData) + 若干视频 tag。

    spans: [(时间戳 ms, 载荷字节数)]。载荷给大是为了让「体积 ÷ 媒体时长」落在
    真实量级 —— 百来字节的文件算出来的 kbps 会四舍五入成 0,测不出东西。
    """
    body = _amf_str("onMetaData") + b"\x08" + struct.pack(">I", 3)
    body += _amf_key("duration") + _amf_num(120.0)
    if width is not None:
        body += _amf_key("width") + _amf_num(width)
    if height is not None:
        body += _amf_key("height") + _amf_num(height)
    body += b"\x00\x00\x09"
    spans = spans if spans is not None else [(0, 8)]
    with open(path, "wb") as f:
        f.write(b"FLV\x01\x05\x00\x00\x00\x09")
        f.write(struct.pack(">I", 0))
        f.write(_flv_tag(18, 0, body))
        for ts_ms, nbytes in spans:
            f.write(_flv_tag(9, ts_ms, b"\x17" * nbytes))


def _kbps(nbytes, seconds):
    return int(round(float(nbytes) * 8.0 / float(seconds) / 1000.0))


class TestMediaInfo(unittest.TestCase):
    """monitor._media_info:分辨率来自文件头,码率来自体积÷时长。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        monitor._DIM_CACHE.clear()

    def tearDown(self):
        monitor._DIM_CACHE.clear()
        self.tmp.cleanup()

    def test_single_file(self):
        p = os.path.join(self.d, "主播A-20261028-010000.flv")
        _make_flv(p, 1920, 1080)
        n = os.path.getsize(p)
        mi = monitor._media_info(p, n, 120.0)
        self.assertEqual((mi["w"], mi["h"]), (1920, 1080))
        self.assertEqual(mi["kbps"], _kbps(n, 120.0))
        self.assertNotIn("tail", mi)

    def test_portrait_kept_as_is(self):
        """竖屏就是 1080×1920,不能被"规整"成横屏 —— 报错方向会误导排查。"""
        p = os.path.join(self.d, "竖屏.flv")
        _make_flv(p, 1080, 1920)
        mi = monitor._media_info(p, 1000000, 60.0)
        self.assertEqual((mi["w"], mi["h"]), (1080, 1920))

    def test_segments_uniform_no_tail(self):
        tmpl = os.path.join(self.d, "主播A-20261028-010000-%03d.flv")
        for i in (0, 1):
            _make_flv(tmpl % i, 1088, 1920)
        seg = monitor._segment_stats(tmpl)
        mi = monitor._media_info(tmpl, 2000000, 300.0, seg=seg)
        self.assertEqual((mi["w"], mi["h"]), (1088, 1920))
        self.assertNotIn("tail", mi)

    def test_segments_degraded_tail(self):
        """中途自动降级:首片 1080p、末片 720p,必须带出 tail 而不是只报首片。"""
        tmpl = os.path.join(self.d, "主播B-20261028-020000-%03d.flv")
        _make_flv(tmpl % 0, 1088, 1920)
        _make_flv(tmpl % 1, 720, 1280)
        seg = monitor._segment_stats(tmpl)
        mi = monitor._media_info(tmpl, 3000000, 600.0, seg=seg)
        self.assertEqual((mi["w"], mi["h"]), (1088, 1920))
        self.assertEqual(mi["tail"], [720, 1280])

    def test_tail_false_skips_last_segment(self):
        """webui 存量补算对 NAS 只读首片(tail=False),多一次 SMB open 不值得。"""
        tmpl = os.path.join(self.d, "主播B-20261028-020000-%03d.flv")
        _make_flv(tmpl % 0, 1088, 1920)
        _make_flv(tmpl % 1, 720, 1280)
        seg = monitor._segment_stats(tmpl)
        mi = monitor._media_info(tmpl, 3000000, 600.0, seg=seg, tail=False)
        self.assertNotIn("tail", mi)

    def test_no_seg_still_finds_tail(self):
        """没传 seg(_segment_stats 结果)时靠 find_segments 兜底,不能因此丢掉 tail。"""
        tmpl = os.path.join(self.d, "主播C-20261028-030000-%03d.flv")
        _make_flv(tmpl % 0, 1088, 1920)
        _make_flv(tmpl % 1, 720, 1280)
        mi = monitor._media_info(tmpl, 3000000, 600.0)
        self.assertEqual(mi["tail"], [720, 1280])

    def test_missing_file_degrades_to_kbps(self):
        """文件已删/归档未挂载:分辨率不知道,但体积与时长还在,码率照样能报。"""
        p = os.path.join(self.d, "已删除.flv")
        mi = monitor._media_info(p, 1100000, 60.0)
        self.assertEqual(mi, {"kbps": _kbps(1100000, 60.0)})

    def test_non_flv_has_no_dims(self):
        p = os.path.join(self.d, "重封装.mp4")
        with open(p, "wb") as f:
            f.write(b"\x00\x00\x00\x18ftypisom" + b"\x00" * 64)
        mi = monitor._media_info(p, 500000, 10.0)
        self.assertNotIn("w", mi)
        self.assertIn("kbps", mi)

    def test_dims_without_size_or_duration(self):
        p = os.path.join(self.d, "无体积.flv")
        _make_flv(p, 1920, 1080)
        mi = monitor._media_info(p, None, None)
        self.assertEqual((mi["w"], mi["h"]), (1920, 1080))
        self.assertNotIn("kbps", mi)

    def test_all_empty_returns_none(self):
        self.assertIsNone(monitor._media_info("", None, None))
        self.assertIsNone(monitor._media_info(os.path.join(self.d, "没有.mp4")))

    def test_zero_size_does_not_produce_kbps(self):
        p = os.path.join(self.d, "零体积.flv")
        _make_flv(p, 1920, 1080)
        mi = monitor._media_info(p, 0, 0)
        self.assertNotIn("kbps", mi)

    def test_dims_cached_after_first_read(self):
        """历史页 5 秒一轮,归档后每次 open 都是 SMB;成功后必须命中缓存。"""
        p = os.path.join(self.d, "缓存.flv")
        _make_flv(p, 1920, 1080)
        monitor._video_dims(p)
        self.assertIn(p, monitor._DIM_CACHE)
        os.remove(p)                        # 文件没了但缓存仍在 → 不再回盘
        self.assertEqual(monitor._video_dims(p), (1920, 1080))

    def test_failed_read_not_cached(self):
        """读不到**不能**缓存:录制中的首片 onMetaData 可能还没落盘。"""
        p = os.path.join(self.d, "迟到.flv")
        self.assertIsNone(monitor._video_dims(p))
        self.assertNotIn(p, monitor._DIM_CACHE)
        _make_flv(p, 1088, 1920)            # 稍后才写好
        self.assertEqual(monitor._video_dims(p), (1088, 1920))


class TestHistoryFinishWritesMedia(unittest.TestCase):
    """收尾时就把 media 写进 history.json —— 那一刻文件还在本机,读它最便宜。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.old_hist = monitor.HISTORY_PATH
        monitor.HISTORY_PATH = os.path.join(self.base, "history.json")
        monitor._history = []
        monitor._DIM_CACHE.clear()
        self.sess = os.path.join(self.base, "recordings", "主播A", "2026-10-28")
        os.makedirs(self.sess, exist_ok=True)
        self.tmpl = os.path.join(self.sess, "主播A-20261028-010000-%03d.flv")

    def tearDown(self):
        monitor.HISTORY_PATH = self.old_hist
        monitor._history = []
        monitor._DIM_CACHE.clear()
        self.tmp.cleanup()

    def _add(self, segs=1):
        for i in range(segs):
            _make_flv(self.tmpl % i, 1088, 1920,
                      spans=[(0, 8), (60000, 1 << 20)])
        monitor._history = [{
            "name": "主播A",
            "started_at": time.time() - 300,
            "output_path": self.tmpl,
            "status": "recording",
        }]
        return monitor._history[0]

    def test_finish_writes_media(self):
        self._add(segs=2)
        monitor._history_finish(None, "主播A")
        e = monitor._history[0]
        self.assertEqual(e["status"], "done")
        mi = e.get("media") or {}
        self.assertEqual((mi.get("w"), mi.get("h")), (1088, 1920))
        self.assertGreater(mi.get("kbps", 0), 0)
        # 码率必须与「体积 ÷ 媒体时长」自洽(时长在本文件里只有几毫秒,故只校验量级)
        self.assertLess(mi["kbps"], 100000)

    def test_finish_keeps_other_fields(self):
        """新增字段不能挤掉原有字段(段数/容量是历史页别处依赖的)。"""
        self._add(segs=2)
        monitor._history_finish(None, "主播A")
        e = monitor._history[0]
        self.assertEqual(e["segments"], 2)
        self.assertGreater(e["size"], 0)
        self.assertIn("duration", e)

    def test_finish_without_files_still_succeeds(self):
        """素材已被删:收尾不能抛异常,只是没有 media 字段。"""
        monitor._history = [{
            "name": "主播A",
            "started_at": time.time() - 10,
            "output_path": self.tmpl,
            "status": "recording",
        }]
        monitor._history_finish(None, "主播A")
        self.assertEqual(monitor._history[0]["status"], "done")
        self.assertNotIn("media", monitor._history[0])


class FakeState:
    def __init__(self, cfg):
        self.cfg = cfg
        self.history = []

    def get_config(self):
        return self.cfg

    def set_history(self, h):
        self.history = h


class TestBackfillMediaInfo(unittest.TestCase):
    """存量条目(上线前录的)由 /api/history 惰性补算,分本地/远端限额。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.old_hist = monitor.HISTORY_PATH
        monitor.HISTORY_PATH = os.path.join(self.base, "history.json")
        monitor._history = []
        monitor._DIM_CACHE.clear()
        self.rec = os.path.join(self.base, "recordings")
        self.sess = os.path.join(self.rec, "主播A", "2026-10-01")
        os.makedirs(self.sess, exist_ok=True)
        self.cfg = {"recorder": {"output_dir": self.rec},
                    "nas": {"enabled": False}, "archive": {"mode": "auto"}}
        self.state = FakeState(self.cfg)

    def tearDown(self):
        monitor.HISTORY_PATH = self.old_hist
        monitor._history = []
        monitor._DIM_CACHE.clear()
        self.tmp.cleanup()

    def _entry(self, ts, dims=(1088, 1920)):
        tmpl = os.path.join(self.sess, "主播A-20261001-%06d-%%03d.flv" % ts)
        _make_flv(tmpl % 0, *dims, spans=[(0, 8), (60000, 200000)])
        return {"name": "主播A", "started_at": float(ts), "output_path": tmpl,
                "status": "done", "duration": 100.0,
                "size": os.path.getsize(tmpl % 0), "segments": 1}

    def test_backfill_fills_media(self):
        monitor._history = [self._entry(100)]
        self.assertTrue(webui._backfill_media_info(self.state))
        mi = monitor._history[0]["media"]
        self.assertEqual((mi["w"], mi["h"]), (1088, 1920))
        self.assertGreater(mi["kbps"], 0)
        # 补完要落盘 + 同步前端 state(只改内存会留下过期快照)
        self.assertTrue(os.path.exists(monitor.HISTORY_PATH))
        self.assertEqual(len(self.state.history), 1)

    def test_backfill_skips_entries_with_media(self):
        e = self._entry(100)
        e["media"] = {"w": 1920, "h": 1080, "kbps": 1200}
        monitor._history = [e]
        self.assertFalse(webui._backfill_media_info(self.state))
        self.assertEqual(monitor._history[0]["media"], {"w": 1920, "h": 1080, "kbps": 1200})

    def test_backfill_skips_recording(self):
        e = self._entry(100)
        e["status"] = "recording"
        monitor._history = [e]
        self.assertFalse(webui._backfill_media_info(self.state))
        self.assertNotIn("media", monitor._history[0])

    def test_backfill_respects_caps(self):
        """限额:一次请求只补一小批,不能把 /api/history 拖成 43 秒那次。"""
        n = webui._MEDIA_LAZY_LOCAL_CAP + 5
        monitor._history = [self._entry(100 + i) for i in range(n)]
        webui._backfill_media_info(self.state)
        filled = sum(1 for e in monitor._history if e.get("media"))
        self.assertEqual(filled, webui._MEDIA_LAZY_LOCAL_CAP)
        # 下一轮接着补,不会永远卡在同一批
        webui._backfill_media_info(self.state)
        self.assertEqual(sum(1 for e in monitor._history if e.get("media")), n)

    def test_backfill_missing_files_is_noop(self):
        e = self._entry(100)
        os.remove(e["output_path"].replace("%03d", "000"))
        monitor._history = [e]
        self.assertFalse(webui._backfill_media_info(self.state))
        self.assertNotIn("media", monitor._history[0])


class TestHistoryRowUi(unittest.TestCase):
    """前端守卫:历史行真的把这两个胶囊画出来了,而不是只有后端字段。"""

    def setUp(self):
        with open(HTML, encoding="utf-8") as f:
            self.html = f.read()

    def test_pills_rendered(self):
        self.assertIn('class="h-pill vres"', self.html)
        self.assertIn('class="h-pill vbr"', self.html)

    def test_pills_in_row_template(self):
        """必须在 h-pills 那一行里,顺序在体积之后、状态徽章之前。"""
        m = [ln for ln in self.html.splitlines() if 'class="h-pills"' in ln
             and "durPill" in ln]
        self.assertTrue(m, "没找到历史行的胶囊容器")
        row = m[0]
        self.assertIn("${sizePill}${dimPill}${brPill}", row)
        self.assertLess(row.index("${sizePill}"), row.index("${dimPill}"))
        self.assertLess(row.index("${dimPill}"), row.index("${brPill}"))

    def test_pill_styles_defined(self):
        """胶囊样式必须在:少了 .vres/.vbr 就退化成灰色,和体积糊成一片看不出来。"""
        self.assertIn(".h-pill.vres { color: var(--text); }", self.html)
        self.assertIn(".h-pill.vbr { color: var(--accent-2); }", self.html)

    def test_narrow_layout_wraps_pills(self):
        """窄窗口必须让胶囊换行。

        5 个文字胶囊把历史行的 min-content 撑到 ~1085px,1080p 屏幕全屏浏览器
        (1080px)会冒出横向滚动条、右侧按钮被推出视口 —— 实测 1080 溢出、1120 正常。
        2026-10-30 礼物胶囊加了百分比(57 → 86px,带满分圆点 95px),min-content 升到
        ~1115 / 1124px。
        ⚠️ 断点按**视口**判,但行可用宽 = 视口 − 220px(148 aside + 48 main padding
        + 24 行 padding);旧值 1179 漏扣这 220px,导致 1180~1343 整段既不换行又放不下。
        2026-11-01 时间列精简到「月-日」(148 → 33px)后 min-content 回落到 ~1009px,
        断点 = 1009 + 220 + 20 余量 = 1249。
        ⚠️ 必须落在**媒体查询内**:写成全局规则会让宽屏也换行(行高抖动)。
        定位要带上 `.h-main`,不能只匹配 `@media (...)` —— 项目里另有同族断点的
        `#streamer-list` 规则。
        """
        m = re.search(r"@media \(max-width: 1249px\) \{\n\s*\.h-main \{ flex-wrap: wrap"
                      r"(.*?)\n\}", self.html, re.S)
        self.assertIsNotNone(m, "缺少窄窗口换行规则(或被改成全局/换了断点)")
        self.assertIn(".h-pills { order: 3; flex-basis: 100%; }", m.group(1))
        self.assertIn(".h-ops { order: 2; }", m.group(1))

    def test_fmt_kbps_units(self):
        """码率口径:<1000 报 kbps,≥1000 折 Mbps 并去掉多余的 0。"""
        self.assertIn("function fmtKbps", self.html)
        self.assertIn('return k + " kbps"', self.html)
        self.assertIn('" Mbps"', self.html)

    def test_recording_rows_have_no_pills(self):
        """录制中的场次还没收尾,不报(避免显示上一场的残留数字)。"""
        self.assertIn("if (!recording && mi && mi.w && mi.h)", self.html)
        self.assertIn("if (!recording && mi && mi.kbps > 0)", self.html)


if __name__ == "__main__":
    unittest.main()
