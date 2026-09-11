#!/usr/bin/env python3
"""「自动录制」开关与主播备注的单元测试。

背景:主播多了以后,多数主播不需要每场都录,只在想录的那一场手动点「开始」。
于是给每个主播加了 auto_record 开关(默认:历史主播 = 开,新加主播 = 关)和
一个 note 备注字段。

这里锁死三件事:
  1. 字段缺失必须按「自动录制」算 —— 否则升级后所有老主播会静默停止录制;
  2. 迁移只补缺失字段,不能把用户已经关掉的主播又打开;
  3. 写入端校验要挡住脏数据(auto_record 非布尔 / note 非字符串或超长)。
"""
import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import monitor  # noqa: E402
import webui  # noqa: E402


class TestAutoRecordEnabled(unittest.TestCase):
    """是否「开播即录」的判定。"""

    def test_missing_field_means_on(self):
        """历史主播没有这个字段 -> 按原来的行为(自动录)处理。"""
        cfg = {"monitors": [{"name": "甲", "anchor": "1"}]}
        self.assertTrue(monitor.auto_record_enabled(cfg, "甲"))

    def test_true(self):
        cfg = {"monitors": [{"name": "甲", "auto_record": True}]}
        self.assertTrue(monitor.auto_record_enabled(cfg, "甲"))

    def test_false(self):
        cfg = {"monitors": [{"name": "甲", "auto_record": False}]}
        self.assertFalse(monitor.auto_record_enabled(cfg, "甲"))

    def test_other_streamer_unaffected(self):
        cfg = {"monitors": [{"name": "甲", "auto_record": False},
                            {"name": "乙", "auto_record": True}]}
        self.assertFalse(monitor.auto_record_enabled(cfg, "甲"))
        self.assertTrue(monitor.auto_record_enabled(cfg, "乙"))

    def test_unknown_name_defaults_on(self):
        """主播不在配置里(刚删/改名):保守按「录」处理,不会漏录。"""
        self.assertTrue(monitor.auto_record_enabled({"monitors": [{"name": "甲"}]}, "丙"))
        self.assertTrue(monitor.auto_record_enabled({}, "丙"))

    def test_tolerates_non_dict_entry(self):
        cfg = {"monitors": ["坏了", {"name": "甲", "auto_record": False}]}
        self.assertFalse(monitor.auto_record_enabled(cfg, "甲"))


class TestMigrateAutoRecord(unittest.TestCase):
    """一次性迁移:只补缺失字段。"""

    def test_fills_missing_with_true(self):
        cfg = {"monitors": [{"name": "甲"}, {"name": "乙", "auto_record": False}]}
        self.assertTrue(monitor._migrate_auto_record(cfg))
        self.assertTrue(cfg["monitors"][0]["auto_record"])
        self.assertFalse(cfg["monitors"][1]["auto_record"])  # 已关的不能被打开

    def test_no_change_returns_false(self):
        cfg = {"monitors": [{"name": "甲", "auto_record": False}]}
        self.assertFalse(monitor._migrate_auto_record(cfg))

    def test_empty_monitors(self):
        cfg = {"monitors": []}
        self.assertFalse(monitor._migrate_auto_record(cfg))

    def test_load_config_persists_migration(self):
        """迁移结果要落盘,否则下次启动又要重来一遍。"""
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "config.json")
            with open(path, "w", encoding="utf-8") as f:
                json.dump({"monitors": [{"name": "甲", "anchor": "1"}]}, f)
            orig = monitor.CONFIG_PATH
            monitor.CONFIG_PATH = path
            try:
                cfg = monitor.load_config()
            finally:
                monitor.CONFIG_PATH = orig
            self.assertTrue(cfg["monitors"][0]["auto_record"])
            with open(path, encoding="utf-8") as f:
                on_disk = json.load(f)
            self.assertTrue(on_disk["monitors"][0]["auto_record"])


class TestDormant(unittest.TestCase):
    """关掉自动录制 = 休眠:连检测都不跑(省请求、省 Cookie 配额)。"""

    def test_off_and_idle_is_dormant(self):
        cfg = {"monitors": [{"name": "甲", "auto_record": False}]}
        self.assertTrue(monitor.is_dormant(cfg, "甲", False))

    def test_on_is_not_dormant(self):
        cfg = {"monitors": [{"name": "甲", "auto_record": True}]}
        self.assertFalse(monitor.is_dormant(cfg, "甲", False))

    def test_missing_field_is_not_dormant(self):
        """老主播没这个字段 = 还在自动录,绝不能悄悄休眠。"""
        self.assertFalse(monitor.is_dormant({"monitors": [{"name": "甲"}]}, "甲", False))

    def test_recording_is_never_dormant(self):
        """手动开的录制必须继续检测:下播收尾、归档都靠检测循环发现。"""
        cfg = {"monitors": [{"name": "甲", "auto_record": False}]}
        self.assertFalse(monitor.is_dormant(cfg, "甲", True))


class TestWebuiValidation(unittest.TestCase):
    """写入端校验:挡脏数据,放过正常数据。"""

    def setUp(self):
        self.state = webui.State("/tmp/never-written.json",
                                 {"monitors": [{"name": "甲"}]}, None)

    def _err(self, patch):
        return self.state._validate(patch)

    def test_auto_record_ok(self):
        self.assertIsNone(self._err({"monitors": [
            {"name": "甲", "auto_record": False, "note": "只看不录"}]}))

    def test_auto_record_must_be_bool(self):
        self.assertIn("auto_record", self._err(
            {"monitors": [{"name": "甲", "auto_record": "yes"}]}) or "")
        self.assertIsNone(self._err({"monitors": [{"name": "甲", "auto_record": True}]}))

    def test_note_must_be_string(self):
        self.assertIn("note", self._err(
            {"monitors": [{"name": "甲", "note": 123}]}) or "")

    def test_note_length_limit(self):
        self.assertIn("最多 200 字", self._err(
            {"monitors": [{"name": "甲", "note": "x" * 201}]}) or "")
        self.assertIsNone(self._err({"monitors": [{"name": "甲", "note": "x" * 200}]}))

    def test_monitors_must_be_list(self):
        self.assertIn("必须是数组", self._err({"monitors": {"name": "甲"}}) or "")

    def test_entry_must_be_object(self):
        self.assertIn("每一项必须是对象", self._err({"monitors": ["甲"]}) or "")

    def test_new_streamer_without_field_passes(self):
        """没带 auto_record 也要能存 —— 判定端会按 True 兜底。"""
        self.assertIsNone(self._err({"monitors": [{"name": "新主播", "anchor": "2"}]}))


if __name__ == "__main__":
    unittest.main()
