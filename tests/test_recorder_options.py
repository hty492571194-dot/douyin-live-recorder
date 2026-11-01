# -*- coding: utf-8 -*-
"""录制选项(下拉化)的后端校验 + UI/后端一致性守卫。

- format 白名单: flv / mp4 / ts(界面下拉与 API 直调同一条校验,
  防「拼错=ffmpeg 启动即败」)
- preset 白名单: monitor.X264_PRESETS(UI 精简为 4 档,后端保持 9 档
  向后兼容 —— 旧配置里的 slow/veryslow 等值不被新校验打死)
- 一致性守卫: index.html 的选项列表与后端白名单保持同步,
  防「UI 加了档位但后端拒收」或「后端砍了档位但 UI 还在展示」
"""
import json
import os
import re
import sys
import tempfile
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

import webui
import monitor


def _make_state(tmpdir):
    cfg = {
        "monitors": [],
        "recorder": {"output_dir": "", "enabled": True, "format": "flv",
                     "bitrate": "1900", "segment_time": 2300, "preset": "veryfast"},
    }
    path = os.path.join(tmpdir, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False)
    return webui.State(path, cfg, store=None)


class TestRecorderValidate(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.state = _make_state(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    # ── format ──────────────────────────────────────────────
    def test_format_all_three_valid(self):
        for fmt in ("flv", "mp4", "ts"):
            res = self.state.update_config({"recorder": {"format": fmt}})
            self.assertTrue(res["ok"], "%s 应合法: %s" % (fmt, res.get("error")))

    def test_format_invalid_rejected(self):
        for bad in ("avi", "mkv", "mv4", "", "flv ", "FLV"):
            res = self.state.update_config({"recorder": {"format": bad}})
            self.assertFalse(res["ok"], "format=%r 应被拒" % (bad,))
            self.assertIn("format", res.get("error") or "")

    def test_format_absent_kept_unchanged(self):
        res = self.state.update_config({"recorder": {"bitrate": "2000"}})
        self.assertTrue(res["ok"])
        self.assertEqual(self.state.config["recorder"]["format"], "flv")

    # ── preset ──────────────────────────────────────────────
    def test_preset_ui_four_all_valid(self):
        for p in ("ultrafast", "veryfast", "fast", "medium"):
            res = self.state.update_config({"recorder": {"preset": p}})
            self.assertTrue(res["ok"], "%s 应合法: %s" % (p, res.get("error")))

    def test_preset_legacy_values_still_valid(self):
        # 后端白名单保持 9 档:旧配置/历史值不被新校验打死
        for p in ("superfast", "faster", "slow", "slower", "veryslow"):
            res = self.state.update_config({"recorder": {"preset": p}})
            self.assertTrue(res["ok"], "%s(旧值)应保持合法" % p)

    def test_preset_invalid_rejected(self):
        res = self.state.update_config({"recorder": {"preset": "nope"}})
        self.assertFalse(res["ok"])
        self.assertIn("preset", res.get("error") or "")

    def test_preset_empty_ok(self):
        # 空 = 未提交语义(历史行为),不报错
        res = self.state.update_config({"recorder": {"preset": ""}})
        self.assertTrue(res["ok"])


class TestUiBackendConsistency(unittest.TestCase):
    """守卫:index.html 的选项列表与后端白名单/校验保持一致。"""

    @classmethod
    def setUpClass(cls):
        p = os.path.join(BASE, "static", "index.html")
        with open(p, encoding="utf-8") as f:
            cls.html = f.read()

    def _values(self, name):
        m = re.search(r"const %s = \[(.*?)\];" % name, self.html, re.S)
        self.assertIsNotNone(m, "%s 未找到" % name)
        return re.findall(r'\["([a-zA-Z0-9_]+)",', m.group(1))

    def test_preset_options_subset_of_backend_whitelist(self):
        vals = self._values("PRESET_OPTIONS")
        self.assertEqual(len(vals), 4, "preset 应精简为 4 档")
        for v in vals:
            self.assertIn(v, monitor.X264_PRESETS, "%s 不在后端白名单" % v)

    def test_default_preset_present_in_ui(self):
        default = (monitor.DEFAULT_CONFIG.get("recorder") or {}).get("preset")
        self.assertIn(default, self._values("PRESET_OPTIONS"),
                      "默认档 %s 必须保留在 UI 选项里" % default)

    def test_format_options_match_backend(self):
        vals = self._values("FORMAT_OPTIONS")
        self.assertEqual(vals, ["flv", "mp4", "ts"])

    def test_every_option_has_desc(self):
        # 每个选项都要带说明文案(方案1 说明卡的数据源)
        for name in ("PRESET_OPTIONS", "FORMAT_OPTIONS"):
            m = re.search(r"const %s = \[(.*?)\];" % name, self.html, re.S)
            pairs = re.findall(r'\["([a-zA-Z0-9_]+)",\s*"([^"]{8,})"\]', m.group(1))
            self.assertEqual(len(pairs), len(self._values(name)),
                             "%s 有选项缺说明文案" % name)

    def test_save_hint_says_next_recording(self):
        # 录制设置卡保存提示需保持「下次开始录制时生效」语义
        self.assertIn("新参数在下次开始录制时生效", self.html)


if __name__ == "__main__":
    unittest.main()
