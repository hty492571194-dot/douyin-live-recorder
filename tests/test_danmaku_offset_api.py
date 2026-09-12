# -*- coding: utf-8 -*-
"""弹幕/礼物偏移的写入端契约。

覆盖三处:
- `webui._apply_one_offset_group`:单组偏移写入(弹幕与礼物各走一组)
- `webui.State._validate`:配置项 `danmaku.gift_offset_seconds` 的类型与范围
- `webui._danmaku_apply_offset`:端到端落盘 align.json 并从 jsonl 重建 .ass

礼物偏移是「叠加在弹幕偏移之上」的额外量,默认 0 时行为与旧版完全一致
(旧版只有一组偏移),所以这里同时守住旧的 global_offset/per_segment 不受影响。
"""
import json
import os
import struct
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import subtitle
import webui


def _flv_tag(tag_type: int, ts_ms: int, data_len: int = 8) -> bytes:
    lower = ts_ms & 0xFFFFFF
    ext = (ts_ms >> 24) & 0xFF
    header = bytes([tag_type]) + struct.pack(">I", data_len)[1:] + \
        struct.pack(">I", lower)[1:] + bytes([ext]) + b"\x00\x00\x00"
    return header + b"\x00" * data_len + struct.pack(">I", 11 + data_len)


def _make_flv(path: str, tags):
    with open(path, "wb") as f:
        f.write(b"FLV\x01\x05\x00\x00\x00\x09")
        f.write(struct.pack(">I", 0))
        for t, ts in tags:
            f.write(_flv_tag(t, ts))


class TestOffsetGroupWrite(unittest.TestCase):
    """`_apply_one_offset_group` 的纯函数行为(不碰磁盘)。"""

    def test_writes_gift_group(self):
        align = {}
        err = webui._apply_one_offset_group(
            align, {"gift_global_offset": 2.5, "gift_per_segment": {"000": -1}},
            "gift_global_offset", "gift_per_segment", "礼物偏移")
        self.assertIsNone(err)
        self.assertEqual(align["gift_global_offset"], 2.5)
        self.assertEqual(align["gift_per_segment"], {"000": -1.0})

    def test_absent_key_is_noop(self):
        """body 里没有该组键时不得新建字段 —— 面板只调弹幕偏移时不能动礼物。"""
        align = {}
        self.assertIsNone(webui._apply_one_offset_group(
            align, {"global_offset": 1}, "gift_global_offset",
            "gift_per_segment", "礼物偏移"))
        self.assertEqual(align, {})

    def test_null_clears(self):
        align = {"gift_global_offset": 3, "gift_per_segment": {"000": 1}}
        self.assertIsNone(webui._apply_one_offset_group(
            align, {"gift_global_offset": None, "gift_per_segment": {}},
            "gift_global_offset", "gift_per_segment", "礼物偏移"))
        self.assertNotIn("gift_global_offset", align)
        self.assertEqual(align["gift_per_segment"], {})

    def test_range_and_type_errors(self):
        align = {}
        self.assertIn("必须是数值", webui._apply_one_offset_group(
            align, {"gift_global_offset": "abc"}, "gift_global_offset",
            "gift_per_segment", "礼物偏移") or "")
        self.assertIn("±3600", webui._apply_one_offset_group(
            align, {"gift_global_offset": 3601}, "gift_global_offset",
            "gift_per_segment", "礼物偏移") or "")
        self.assertIn("必须是对象", webui._apply_one_offset_group(
            align, {"gift_per_segment": [1]}, "gift_global_offset",
            "gift_per_segment", "礼物偏移") or "")
        self.assertIn("礼物偏移必须是数值", webui._apply_one_offset_group(
            align, {"gift_per_segment": {"000": "x"}}, "gift_global_offset",
            "gift_per_segment", "礼物偏移") or "")
        # 出错时不得留下已生效的偏移值(调用方在报错分支直接返回,不落盘)
        self.assertNotIn("gift_global_offset", align)
        self.assertEqual(align.get("gift_per_segment") or {}, {})

    def test_danmaku_group_messages_unchanged(self):
        """旧错误串保持原样(前端与文档都按原措辞描述)。"""
        align = {}
        self.assertEqual(webui._apply_one_offset_group(
            align, {"global_offset": "abc"}, "global_offset", "per_segment", "偏移"),
            "global_offset 必须是数值")
        self.assertEqual(webui._apply_one_offset_group(
            align, {"per_segment": {"001": "x"}}, "global_offset", "per_segment", "偏移"),
            "分片 001 偏移必须是数值")


class TestConfigValidation(unittest.TestCase):
    """配置写入端:`danmaku.gift_offset_seconds` 的类型与范围。"""

    def setUp(self):
        self.state = webui.State("/tmp/never-written.json", {"monitors": []}, None)

    def test_gift_offset_ok(self):
        self.assertIsNone(self.state._validate({"danmaku": {"gift_offset_seconds": -2.5}}))
        self.assertIsNone(self.state._validate({"danmaku": {"gift_offset_seconds": 0}}))
        self.assertIsNone(self.state._validate({"danmaku": {"gift_offset_seconds": 3600}}))

    def test_gift_offset_must_be_number(self):
        self.assertIn("gift_offset_seconds", self.state._validate(
            {"danmaku": {"gift_offset_seconds": "1"}}) or "")
        # 布尔是 int 的子类,必须单独挡掉,否则 true 会变成 1
        self.assertIn("gift_offset_seconds", self.state._validate(
            {"danmaku": {"gift_offset_seconds": True}}) or "")

    def test_gift_offset_range(self):
        self.assertIn("取值范围", self.state._validate(
            {"danmaku": {"gift_offset_seconds": 3601}}) or "")
        self.assertIn("取值范围", self.state._validate(
            {"danmaku": {"gift_offset_seconds": -3601}}) or "")


class TestApplyOffsetEndToEnd(unittest.TestCase):
    """端到端:POST /api/danmaku/offset 的礼物分支。"""

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.d = self.dir.name
        self.stem = "主播-20260822-140000"
        self.pattern = os.path.join(self.d, self.stem + "-%03d.flv")
        t0 = int(time.time() * 1000)
        self.t0 = t0
        for i in range(2):
            _make_flv(os.path.join(self.d, f"{self.stem}-{i:03d}.flv"),
                      [(9, t0 + i * 600_000), (9, t0 + i * 600_000 + 599_000)])
        self.jsonl = os.path.join(self.d, ".meta", self.stem + ".danmaku.jsonl")
        os.makedirs(os.path.dirname(self.jsonl), exist_ok=True)
        with open(self.jsonl, "w", encoding="utf-8") as f:
            for i in range(2):
                f.write(json.dumps({"ts": t0 + i * 600_000 + 1000, "type": "chat",
                                    "user": "u", "uid": "1",
                                    "content": f"弹幕{i}"}) + "\n")
                f.write(json.dumps({"ts": t0 + i * 600_000 + 2000, "type": "gift",
                                    "user": "老板", "uid": "2", "gift": "嘉年华",
                                    "count": 1, "diamond": 3000}) + "\n")
        # 白名单靠 recorder.output_dir 放行临时目录
        self.state = webui.State("/tmp/never-written.json", {
            "recorder": {"output_dir": self.d},
            "danmaku": {"enabled": True, "offset_seconds": 0, "gift_offset_seconds": 0},
        }, None)

    def tearDown(self):
        self.dir.cleanup()

    def test_gift_offset_roundtrip(self):
        r = webui._danmaku_apply_offset(self.state, {
            "path": self.pattern,
            "gift_global_offset": 2.0,
            "gift_per_segment": {"001": -0.5},
        })
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r["gift_global_offset"], 2.0)
        self.assertEqual(r["config_offset"], 0.0)
        self.assertIsNone(r["global_offset"], "弹幕偏移不应被礼物请求改写")
        segs = {s["idx"]: s for s in r["segments"]}
        # gift_extra = 本分片人工值(对应面板输入框),gift_offset = 含全局的生效值
        self.assertEqual(segs["000"]["gift_extra"], 0.0)
        self.assertEqual(segs["001"]["gift_extra"], -0.5)
        self.assertEqual(segs["000"]["offset"], 0.0)
        # 礼物总偏移 = 弹幕偏移 + 礼物额外
        self.assertEqual(segs["000"]["gift_offset"], 2.0)
        self.assertEqual(segs["001"]["gift_offset"], 1.5)
        with open(subtitle.align_path_for(self.pattern), encoding="utf-8") as f:
            align = json.load(f)
        self.assertEqual(align["gift_global_offset"], 2.0)
        self.assertEqual(align["gift_per_segment"], {"001": -0.5})
        # 弹幕行时刻不变、礼物行被推后 2s(事件在 anchor+1s / 礼物在 +2s)
        seg000 = os.path.join(self.d, segs["000"]["file"])
        with open(subtitle.ass_path_for_segment(seg000), encoding="utf-8") as f:
            lines = f.read().splitlines()
        chat = [x for x in lines if x.startswith("Dialogue: 0,")]
        gift = [x for x in lines if x.startswith("Dialogue: 1,")]
        self.assertEqual(chat[0].split(",")[1], "0:00:01.00")
        self.assertEqual(gift[0].split(",")[1], "0:00:04.00")

    def test_clear_gift_offset(self):
        webui._danmaku_apply_offset(self.state, {"path": self.pattern,
                                                 "gift_global_offset": 3.0})
        r = webui._danmaku_apply_offset(self.state, {
            "path": self.pattern, "gift_global_offset": None,
            "gift_per_segment": {}})
        self.assertTrue(r.get("ok"), r)
        self.assertIsNone(r["gift_global_offset"])
        self.assertEqual(r["segments"][0]["gift_extra"], 0.0)

    def test_danmaku_offset_still_works(self):
        """旧调用方式(只带 global_offset)必须照旧生效。"""
        r = webui._danmaku_apply_offset(self.state, {"path": self.pattern,
                                                     "global_offset": 1.5})
        self.assertTrue(r.get("ok"), r)
        self.assertEqual(r["global_offset"], 1.5)
        self.assertEqual(r["segments"][0]["offset"], 1.5)
        self.assertEqual(r["segments"][0]["gift_extra"], 0.0,
                         "只调弹幕时礼物额外量应为 0")
        self.assertEqual(r["segments"][0]["gift_offset"], 1.5,
                         "礼物跟着弹幕一起动(默认行为)")

    def test_bad_value_rejected(self):
        r = webui._danmaku_apply_offset(self.state, {"path": self.pattern,
                                                     "gift_global_offset": 9999})
        self.assertFalse(r.get("ok"))
        self.assertIn("±3600", r.get("error", ""))


if __name__ == "__main__":
    unittest.main()
