# -*- coding: utf-8 -*-
"""分片统计与「已分 N 段」展示。

长视频按 `recorder.segment_time` 切成多段(文件名 `-000/-001/...`),
界面要能看到当前这场已经分了几段 —— 段数不涨可能卡住了,段数暴涨可能在反复重开。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))

import monitor  # noqa: E402

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(BASE, "static", "index.html"), encoding="utf-8") as _f:
    HTML = _f.read()


class FakeState:
    """只实现 _sync_recording_state 用到的接口。"""

    def __init__(self, cfg=None):
        self.cfg = cfg or {}
        self.recordings = {}

    def get_config(self):
        return self.cfg

    def set_recording(self, name, info):
        self.recordings[name] = info

    def remove_recording(self, name):
        self.recordings.pop(name, None)


class FakeProc:
    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass

    def poll(self):
        return 0


class _SegCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        monitor._rec_carry = {}
        monitor._recordings = {}
        monitor._rec_health = {}

    def tearDown(self):
        monitor._rec_carry = {}
        monitor._recordings = {}
        monitor._rec_health = {}
        self.tmp.cleanup()

    def _write(self, name, size):
        p = os.path.join(self.d, name)
        with open(p, "wb") as f:
            f.write(b"x" * size)
        return p

    def _pattern(self, stem="主播甲-20260918-120000"):
        return os.path.join(self.d, stem + "-%03d.flv")


class TestSegmentStats(_SegCase):
    def test_counts_segments_of_current_prefix(self):
        pat = self._pattern()
        for i, sz in enumerate((1024, 2048, 4096)):
            self._write("主播甲-20260918-120000-%03d.flv" % i, sz)
        s = monitor._segment_stats(pat)
        self.assertEqual(s["count"], 3)
        self.assertEqual(s["bytes"], 1024 + 2048 + 4096)
        self.assertEqual(s["index"], 2)
        self.assertEqual(s["latest"], "主播甲-20260918-120000-002.flv")
        self.assertEqual(s["latest_bytes"], 4096)

    def test_ignores_other_sessions_in_same_dir(self):
        """同一天同目录会有多个场次,只认当前前缀。"""
        pat = self._pattern()
        self._write("主播甲-20260918-120000-000.flv", 100)
        self._write("主播甲-20260918-120000-001.flv", 100)
        for i in range(4):                       # 另一场(不同时间戳)
            self._write("主播甲-20260918-093000-%03d.flv" % i, 100)
        self.assertEqual(monitor._segment_stats(pat)["count"], 2)

    def test_ignores_other_extensions(self):
        """.ass / .meta 不是分片,不能计入段数。"""
        pat = self._pattern()
        self._write("主播甲-20260918-120000-000.flv", 100)
        self._write("主播甲-20260918-120000-000.ass", 100)
        self._write("主播甲-20260918-120000-001.ass", 100)
        self.assertEqual(monitor._segment_stats(pat)["count"], 1)

    def test_ignores_macos_companion_files(self):
        """exFAT 卷上的 ._ 伴随文件不能算一段。"""
        pat = self._pattern()
        self._write("主播甲-20260918-120000-000.flv", 100)
        self._write("._主播甲-20260918-120000-000.flv", 100)
        self.assertEqual(monitor._segment_stats(pat)["count"], 1)

    def test_four_digit_index_accepted(self):
        """>999 段时 ffmpeg 会写成 4 位,不能漏。"""
        pat = self._pattern()
        self._write("主播甲-20260918-120000-1000.flv", 100)
        self.assertEqual(monitor._segment_stats(pat)["count"], 1)
        self.assertEqual(monitor._segment_stats(pat)["index"], 1000)

    def test_none_when_no_files_yet(self):
        pat = self._pattern()
        s = monitor._segment_stats(pat)
        self.assertIsNotNone(s)
        self.assertEqual(s["count"], 0)
        self.assertEqual(s["bytes"], 0)
        self.assertIsNone(s["latest"])

    def test_none_for_single_file_recording(self):
        """不分片(segment_time=0)时没有"段"的概念,返回 None。"""
        p = os.path.join(self.d, "主播甲-20260918-120000.flv")
        self._write("主播甲-20260918-120000.flv", 100)
        self.assertIsNone(monitor._segment_stats(p))

    def test_none_for_empty_path(self):
        self.assertIsNone(monitor._segment_stats(""))
        self.assertIsNone(monitor._segment_stats(None))

    def test_none_when_dir_missing(self):
        self.assertIsNone(
            monitor._segment_stats("/nonexistent/dir/主播甲-20260918-120000-%03d.flv"))


class TestCarryAcrossRestart(_SegCase):
    """ffmpeg 重开会换新的时间戳前缀,段数必须结转,不能从 1 重新开始。"""

    def test_carry_accumulates_old_prefix(self):
        old = self._pattern("主播甲-20260918-120000")
        self._write("主播甲-20260918-120000-000.flv", 100)
        self._write("主播甲-20260918-120000-001.flv", 100)
        monitor._recordings["主播甲"] = {"output_path": old}
        monitor._carry_segments("主播甲")
        self.assertEqual(monitor._rec_carry["主播甲"]["segments"], 2)
        self.assertEqual(monitor._rec_carry["主播甲"]["bytes"], 200)

    def test_carry_twice(self):
        monitor._recordings["主播甲"] = {"output_path": self._pattern("a-1")}
        self._write("a-1-000.flv", 10)
        monitor._carry_segments("主播甲")
        monitor._recordings["主播甲"] = {"output_path": self._pattern("a-2")}
        self._write("a-2-000.flv", 20)
        monitor._carry_segments("主播甲")
        self.assertEqual(monitor._rec_carry["主播甲"], {"segments": 2, "bytes": 30})

    def test_carry_noop_without_recording(self):
        monitor._carry_segments("不存在的主播")
        self.assertEqual(monitor._rec_carry, {})

    def test_carry_noop_when_single_file(self):
        monitor._recordings["主播甲"] = {"output_path": os.path.join(self.d, "x.flv")}
        monitor._carry_segments("主播甲")
        self.assertEqual(monitor._rec_carry, {})

    def test_stop_clears_carry(self):
        """本场结束,结转清零 —— 否则下一场会带着上一场的段数。"""
        monitor._rec_carry["主播甲"] = {"segments": 3, "bytes": 999}
        monitor._recordings["主播甲"] = {"proc": FakeProc(), "started_at": 1.0,
                                        "output_path": os.path.join(self.d, "x-%03d.flv"),
                                        "stream_url": "u", "format": "flv"}
        st = FakeState()
        monitor._stop_recording(st, "主播甲")
        self.assertNotIn("主播甲", monitor._rec_carry)

    def test_snap_reports_total_including_carry(self):
        pat = self._pattern("主播甲-20260918-130000")
        self._write("主播甲-20260918-130000-000.flv", 500)
        monitor._recordings["主播甲"] = {
            "proc": FakeProc(), "started_at": 1.0, "output_path": pat,
            "stream_url": "u", "format": "flv"}
        monitor._rec_carry["主播甲"] = {"segments": 2, "bytes": 1000}
        st = FakeState()
        monitor._sync_recording_state(st, "主播甲")
        rec = st.recordings["主播甲"]
        self.assertEqual(rec["segments"], 3)          # 2(结转) + 1(当前)
        self.assertEqual(rec["segments_bytes"], 1500)
        self.assertEqual(rec["segment_index"], 0)

    def test_snap_omits_segments_for_single_file(self):
        """单文件录制不给 segments —— 给了会让人以为在分片。"""
        p = os.path.join(self.d, "主播甲-20260918-130000.flv")
        self._write("主播甲-20260918-130000.flv", 100)
        monitor._recordings["主播甲"] = {
            "proc": FakeProc(), "started_at": 1.0, "output_path": p,
            "stream_url": "u", "format": "flv"}
        st = FakeState()
        monitor._sync_recording_state(st, "主播甲")
        self.assertNotIn("segments", st.recordings["主播甲"])


class TestSegmentUi(unittest.TestCase):
    """前端必须真的把段数显示出来(静态守卫)。"""

    def test_consumes_segments_field(self):
        for field in ("rec.segments", "segments_bytes", "segment_file"):
            self.assertIn(field, HTML, "前端没消费 %s" % field)

    def test_shows_segment_count_copy(self):
        # 2026-10-26 四列改版:段数并进卡面状态行(「录制中 · 时长 · N 段 · 分辨率」),
        # 详情页再给出完整段落信息(当前第几段 / 本段进度 / 当前文件)。
        self.assertIn("${segN} 段", HTML)
        self.assertIn('"第 " + segN + " 段"', HTML)

    def test_guarded_by_segment_time(self):
        """没开分片时不该显示段数(避免误导)。"""
        i = HTML.index("const segN = rec.segments;")
        snippet = HTML[i:i + 400]
        self.assertIn("segT > 0", snippet)

    def test_filename_is_escaped(self):
        """文件名含主播名/emoji,渲染前必须转义。"""
        i = HTML.index("rec.segment_file")
        self.assertIn("esc(", HTML[i:i + 160])

    def test_backend_exposes_field(self):
        with open(os.path.join(BASE, "monitor.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertIn('snap["segments"]', src)


if __name__ == "__main__":
    unittest.main()
