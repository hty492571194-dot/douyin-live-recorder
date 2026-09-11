#!/usr/bin/env python3
"""detect.py:sec_uid 识别与探测锚点解析。"""
import sys
import os
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import detect

SEC = "MS4wLjABAAAAwCQqbMRDtGCOY2wY30OHNHl4fbJT8_jM04QAUodzasE"


class TestAnchor(unittest.TestCase):
    def test_is_sec_uid(self):
        self.assertTrue(detect.is_sec_uid(SEC))
        self.assertTrue(detect.is_sec_uid("  " + SEC + "  "))
        self.assertFalse(detect.is_sec_uid("388770149407"))
        self.assertFalse(detect.is_sec_uid(""))
        self.assertFalse(detect.is_sec_uid("随便什么"))

    def test_probe_anchor_web_rid_field(self):
        """sec_uid 锚点 + web_rid 字段 → 用 web_rid 探测(修复核心)。"""
        mon = {"name": "林知夏", "anchor": SEC, "web_rid": "388770149407"}
        probe, err = detect.probe_anchor(mon)
        self.assertEqual(probe, "388770149407")
        self.assertIsNone(err)

    def test_probe_anchor_digit_anchor(self):
        """纯数字锚点直接用。"""
        probe, err = detect.probe_anchor({"name": "x", "anchor": "388770149407"})
        self.assertEqual(probe, "388770149407")
        self.assertIsNone(err)

    def test_probe_anchor_sec_uid_only(self):
        """只有 sec_uid → 明确提示,而不是静默误判未开播。"""
        probe, err = detect.probe_anchor({"name": "林知夏", "anchor": SEC})
        self.assertIsNone(probe)
        self.assertIn("web_rid", err)
        self.assertIn("sec_uid", err)

    def test_probe_anchor_web_rid_precedence(self):
        """web_rid 字段优先于 anchor。"""
        probe, _ = detect.probe_anchor({"name": "x", "anchor": SEC, "web_rid": "111111"})
        self.assertEqual(probe, "111111")


class TestRoomStatus(unittest.TestCase):
    def test_live(self):
        html = r'...\"idStr\":\"7675261780737821478\",\"status\":2,\"ownerUserId\":756...'
        self.assertEqual(detect.extract_room_status(html), 2)

    def test_ended(self):
        html = r'...\"idStr\":\"7675086685574646538\",\"status\":4,\"ownerUserId\":105...'
        self.assertEqual(detect.extract_room_status(html), 4)

    def test_ignores_user_status(self):
        """user 资料的 status 后跟 createTime,不应被当作 room.status。"""
        html = r'...\"city\":\"\",\"status\":1,\"createTime\":0,\"secret\":0...'
        self.assertIsNone(detect.extract_room_status(html))

    def test_plain_json(self):
        """兼容不带转义的原始 JSON。"""
        html = '..."status":2,"ownerUserId":123...'
        self.assertEqual(detect.extract_room_status(html), 2)


class TestStreamUrl(unittest.TestCase):
    def test_audio_stream_excluded(self):
        """纯音频流(only_audio=1,无清晰度后缀)必须排除,返回视频流。"""
        html = (
            r'..."flv":"http://pull-flv-l26.douyincdn.com/stage/stream-408168216488050827.flv'
            r'?expire=6a8d42d3\u0026sign=abc\u0026only_audio=1"...'
            r'..."flv":"http://pull-flv-l26.douyincdn.com/stage/stream-408168216488050827_or4.flv'
            r'?expire=6a8d42d3\u0026sign=xyz"...'
        )
        url, kind = detect.extract_stream_url(html)
        self.assertEqual(kind, "flv")
        self.assertIn("_or4.flv", url)
        self.assertNotIn("only_audio=1", url)
        self.assertNotIn("\\u0026", url)  # 转义已清理
        self.assertIn("&sign=", url)

    def test_or4_preferred(self):
        """多个视频流时优先原画 _or4(忽略出现顺序)。"""
        html = (
            r'..."flv":"http://pull-flv-l26.douyincdn.com/stage/stream-1_Stage0T000ld.flv?expire=x\u0026sign=a"...'
            r'..."flv":"http://pull-flv-l26.douyincdn.com/stage/stream-1_or4.flv?expire=x\u0026sign=b"...'
            r'..."flv":"http://pull-flv-l26.douyincdn.com/stage/stream-1_Stage0T000hd.flv?expire=x\u0026sign=c"...'
        )
        url, kind = detect.extract_stream_url(html)
        self.assertEqual(kind, "flv")
        self.assertIn("_or4.flv", url)

    def test_amp_entity_cleaned(self):
        """HTML 实体 &amp; 也要清理。"""
        html = 'url="http://pull-flv-l26.douyincdn.com/stage/stream-1.flv?expire=x&amp;sign=y"'
        url, kind = detect.extract_stream_url(html)
        self.assertEqual(kind, "flv")
        self.assertIn("&sign=y", url)
        self.assertNotIn("&amp;", url)

    def test_fallback_hls(self):
        """无 flv 时回退 m3u8。"""
        html = '..."hls":"http://pull-hls-l26.douyincdn.com/stage/stream-1_or4.m3u8?expire=x\u0026sign=y"...'
        url, kind = detect.extract_stream_url(html)
        self.assertEqual(kind, "hls")
        self.assertIn(".m3u8", url)

    def test_none(self):
        self.assertEqual(detect.extract_stream_url("no stream here"), (None, None))


if __name__ == "__main__":
    unittest.main()
