#!/usr/bin/env python3
"""存量字幕重生成脚本的自检。

守住的底线:绝不能把存量 .ass 弄坏。存量 .ass 不可再生(jsonl 丢失就没了),
所以脚本必须做到:只处理有事件数据的场次、默认不落盘、落盘前先备份、
重生成后逐文件复核 Dialogue 行数。
"""
import importlib.util
import json
import os
import struct
import sys
import tempfile
import time
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SPEC = importlib.util.spec_from_file_location(
    "regen_subtitles", os.path.join(BASE, "scripts", "regen_subtitles.py"))
regen = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(regen)


def _flv_tag(tag_type, ts_ms, payload):
    header = bytes([tag_type]) + struct.pack(">I", len(payload))[1:] + \
        struct.pack(">I", ts_ms & 0xFFFFFF)[1:] + bytes([(ts_ms >> 24) & 0xFF]) + b"\x00\x00\x00"
    return header + payload + struct.pack(">I", 11 + len(payload))


def _meta(width, height):
    def key(k):
        b = k.encode()
        return struct.pack(">H", len(b)) + b
    body = b"\x02\x00\x0aonMetaData\x08" + struct.pack(">I", 2)
    body += key("width") + b"\x00" + struct.pack(">d", width)
    body += key("height") + b"\x00" + struct.pack(">d", height)
    body += b"\x00\x00\x09"
    return body


def _write_flv(path, w, h, ts_ms, span_ms=590_000):
    """写一个「有长度」的 FLV:onMetaData + 首/末两个视频 tag。

    必须有两个 AV tag —— 只有首 tag 时 flv_last_ts_abs == 首 tag,
    生成器会把分片时长当作 0,所有事件都被 end_ms 过滤掉。
    """
    with open(path, "wb") as f:
        f.write(b"FLV\x01\x05\x00\x00\x00\x09")
        f.write(struct.pack(">I", 0))
        f.write(_flv_tag(18, 0, _meta(w, h)))
        f.write(_flv_tag(9, ts_ms & 0xFFFFFFFF, b"\x00" * 8))
        f.write(_flv_tag(9, (ts_ms + span_ms) & 0xFFFFFFFF, b"\x00" * 8))


class TestHelpers(unittest.TestCase):
    def test_seg_regex(self):
        m = regen.SEG_RE.match("主播甲-20260822-140000-001")
        self.assertEqual(m.group("stem"), "主播甲-20260822-140000")
        self.assertIsNone(regen.SEG_RE.match("主播甲-20260822-140000"))

    def test_find_sessions_dedupes(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        day = os.path.join(d.name, "主播", "2026-09-13")
        os.makedirs(day)
        for i in range(3):
            _write_flv(os.path.join(day, f"主播-20260913-120000-{i:03d}.flv"),
                       1088, 1920, int(time.time() * 1000))
        sessions = regen.find_sessions(d.name)
        self.assertEqual(len(sessions), 1)
        self.assertTrue(sessions[0][0].endswith("主播-20260913-120000-%03d.flv"))

    def test_event_count(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = os.path.join(d.name, "a.jsonl")
        with open(p, "w", encoding="utf-8") as f:
            f.write('{"ts":1}\n\n{"ts":2}\n')
        self.assertEqual(regen.event_count(p), 2)
        self.assertEqual(regen.event_count(os.path.join(d.name, "nope")), 0)

    def test_dialogues(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = os.path.join(d.name, "a.ass")
        with open(p, "w", encoding="utf-8") as f:
            f.write("[Script Info]\nDialogue: 0,x\nDialogue: 1,y\n")
        self.assertEqual(regen.dialogues(p), 2)
        self.assertIsNone(regen.dialogues(os.path.join(d.name, "nope")))

    def test_is_active_since_mtime(self):
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        p = os.path.join(d.name, "a.flv")
        with open(p, "wb") as f:
            f.write(b"FLV")
        segs = [(None, p)]
        self.assertTrue(regen.is_active(segs, 3))       # 刚写过 → 视为在录
        self.assertFalse(regen.is_active(segs, 0))      # 关闭判定
        os.utime(p, (time.time() - 3600, time.time() - 3600))
        self.assertFalse(regen.is_active(segs, 3))      # 1 小时前 → 已结束
        self.assertFalse(regen.is_active([(None, d.name + "/nope.flv")], 3))


class TestEndToEnd(unittest.TestCase):
    """在临时目录上跑完整流程:dry-run 不动文件、apply 后画布正确。"""

    def setUp(self):
        self.d = tempfile.TemporaryDirectory()
        self.addCleanup(self.d.cleanup)
        self.day = os.path.join(self.d.name, "主播", "2026-09-13")
        os.makedirs(self.day)
        self.stem = "主播-20260913-120000"
        t0 = int(time.time() * 1000)
        for i in range(2):
            _write_flv(os.path.join(self.day, f"{self.stem}-{i:03d}.flv"),
                       1088, 1920, t0 + i * 600_000)
        self.jsonl = os.path.join(self.day, ".meta", self.stem + ".danmaku.jsonl")
        os.makedirs(os.path.dirname(self.jsonl), exist_ok=True)
        with open(self.jsonl, "w", encoding="utf-8") as f:
            for i in range(2):
                f.write(json.dumps({"ts": t0 + i * 600_000 + 1000, "type": "chat",
                                    "user": "观众甲", "uid": "1",
                                    "content": f"第{i}片"}, ensure_ascii=False) + "\n")
        # 旧版存量文件:PlayRes 1920x1080
        for i in range(2):
            p = os.path.join(self.day, f"{self.stem}-{i:03d}.ass")
            with open(p, "w", encoding="utf-8") as f:
                f.write("[Script Info]\nScriptType: v4.00+\nPlayResX: 1920\n"
                        "PlayResY: 1080\n\n[Events]\n"
                        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, "
                        "MarginV, Effect, Text\n"
                        "Dialogue: 0,0:00:01.00,0:00:09.00,Danmaku,,0,0,0,,旧内容\n")

    def _run(self, *argv):
        # 夹具的 flv 都是刚写的,关掉「正在录制」判定以免全被跳过;
        # 该判定本身由 TestHelpers.test_is_active_since_mtime 覆盖。
        old = sys.argv
        sys.argv = ["regen_subtitles.py", "--root", self.d.name,
                    "--active-window", "0", *argv]
        try:
            return regen.main()
        finally:
            sys.argv = old

    def _ass(self, i):
        p = os.path.join(self.day, f"{self.stem}-{i:03d}.ass")
        with open(p, encoding="utf-8") as f:
            return f.read()

    def test_dry_run_does_not_touch_files(self):
        before = [self._ass(i) for i in range(2)]
        rc = self._run()
        self.assertEqual(rc, 0)
        self.assertEqual([self._ass(i) for i in range(2)], before)
        self.assertTrue(all("PlayResX: 1920" in c for c in before))

    def test_apply_regenerates_with_video_canvas(self):
        rc = self._run("--apply")
        self.assertEqual(rc, 0)
        c0 = self._ass(0)
        self.assertIn("PlayResX: 1088", c0)
        self.assertIn("PlayResY: 1920", c0)
        self.assertIn("第0片", c0)
        self.assertNotIn("旧内容", c0)

    def test_apply_makes_backup(self):
        self._run("--apply")
        baks = [p for p in os.listdir(self.d.name)
                if p.startswith("_ass_backup-")]
        self.assertTrue(baks)

    def test_session_without_jsonl_skipped(self):
        os.remove(self.jsonl)
        before = [self._ass(i) for i in range(2)]
        rc = self._run("--apply")
        self.assertEqual(rc, 0)
        self.assertEqual([self._ass(i) for i in range(2)], before)

    def test_empty_jsonl_skipped(self):
        with open(self.jsonl, "w", encoding="utf-8"):
            pass
        before = [self._ass(i) for i in range(2)]
        self._run("--apply")
        self.assertEqual([self._ass(i) for i in range(2)], before)

    def test_file_without_old_dialogue_skipped(self):
        """旧文件没有弹幕行 → 不新增也不覆盖(避免凭空造文件)。"""
        p = os.path.join(self.day, f"{self.stem}-002.flv")
        _write_flv(p, 1088, 1920, int(time.time() * 1000) + 1_200_000)
        ass = os.path.join(self.day, f"{self.stem}-002.ass")
        with open(ass, "w", encoding="utf-8") as f:
            f.write("[Script Info]\nPlayResX: 1920\n")
        self._run("--apply")
        with open(ass, encoding="utf-8") as f:
            self.assertNotIn("Dialogue", f.read())

    def test_only_filter(self):
        before = [self._ass(i) for i in range(2)]
        rc = self._run("--apply", "--only", "不存在的名字")
        self.assertEqual(rc, 0)
        self.assertEqual([self._ass(i) for i in range(2)], before)


if __name__ == "__main__":
    unittest.main()
