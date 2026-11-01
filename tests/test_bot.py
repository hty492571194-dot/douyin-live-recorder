#!/usr/bin/env python3
"""企微机器人交互层(bot.py)的守卫测试 —— 菜单模式 + 添加主播三道闸。

handle() 是纯函数(除 _PENDING),全部流程直接实跑;CLI 拉取/发送打桩。
"""
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot  # noqa: E402


class FakeState:
    def __init__(self, monitors=None):
        self.cfg = {"monitors": monitors if monitors is not None else
                    [{"name": "主播甲", "anchor": "testanchor1", "auto_record": True}],
                    "recorder": {"output_dir": os.path.dirname(os.path.abspath(__file__))},
                    "webui": {"host": "127.0.0.1", "port": 8780}}
        self.written = None

    def get_config(self):
        return self.cfg

    def get_status(self):
        return {"recordings": {"主播甲": {"recording": True,
                                        "started_at": time.time() - 3725, "segments": 3}}}

    def get_history(self):
        return []

    def update_config(self, patch):
        self.cfg["monitors"] = patch["monitors"]
        self.written = patch
        return {"ok": True}


def setUpModule():
    bot._PENDING = None


class TestMenu(unittest.TestCase):
    def test_gibberish_returns_menu(self):
        """用户明确要求:不能理解的消息回菜单。"""
        s = FakeState()
        r = bot.handle("今天天气怎么样", s, s.cfg)
        self.assertTrue(r.startswith("🤖"), "兜底必须回菜单")
        for a in bot.ACTIONS:
            if a["key"] != "menu":
                self.assertIn(a["label"], r, f"菜单缺功能: {a['label']}")

    def test_menu_grows_automatically(self):
        """加功能 = 注册表加一项,菜单自动收录(不许手抄菜单文案)。"""
        s = FakeState()
        n_before = bot.menu_text(s.cfg).count("▸")
        bot.ACTIONS.append({"key": "x-test", "label": "测试功能", "patterns": [r"x测试"],
                            "kind": "command", "run": lambda st, c, m: "ok"})
        try:
            self.assertEqual(bot.menu_text(s.cfg).count("▸"), n_before + 1,
                             "新注册的功能必须自动出现在菜单里")
        finally:
            bot.ACTIONS.pop()

    def test_command_is_fullmatch_only(self):
        """command 类整条消息匹配 —— 长句里的关键词不得误触发。"""
        s = FakeState()
        self.assertTrue(bot.handle("我今天没看录制中的回放", s, s.cfg).startswith("🤖"))
        self.assertFalse(bot.handle("录制中", s, s.cfg).startswith("🤖"))


class TestCommands(unittest.TestCase):
    def test_recording(self):
        s = FakeState()
        r = bot.handle("录制中", s, s.cfg)
        self.assertIn("1 路", r)
        self.assertIn("主播甲", r)

    def test_recording_empty(self):
        s = FakeState()
        with mock.patch.object(bot, "_cmd_recording"), \
             mock.patch.object(s, "get_status", return_value={"recordings": {}}):
            r = bot.handle("录制中", s, s.cfg)
        self.assertIn("没有", r)

    def test_disk_and_console(self):
        s = FakeState()
        self.assertIn("剩", bot.handle("磁盘", s, s.cfg))
        self.assertIn("8780", bot.handle("控制台", s, s.cfg))


class TestAddStreamer(unittest.TestCase):
    """添加主播三道闸:格式校验 → 重名校验 → 显式确认。"""

    def test_guide_without_arg(self):
        bot._PENDING = None
        s = FakeState()
        r = bot.handle("添加主播", s, s.cfg)
        self.assertIn("用法", r)
        self.assertIsNone(bot._PENDING, "不带参数只回用法,不进确认流")

    def test_bad_anchor_rejected(self):
        bot._PENDING = None
        s = FakeState()
        r = bot.handle("添加主播 你的小可爱@#", s, s.cfg)
        self.assertIn("不像有效", r)
        self.assertIsNone(bot._PENDING)

    def test_duplicate_anchor_rejected(self):
        bot._PENDING = None
        s = FakeState()
        r = bot.handle("添加主播 testanchor1", s, s.cfg)
        self.assertIn("已经", r)
        self.assertIsNone(bot._PENDING, "重复的不该进确认流")

    def test_confirm_flow_writes_incrementally(self):
        bot._PENDING = None
        s = FakeState()
        r1 = bot.handle("添加主播 li777cm", s, s.cfg)
        self.assertIn("确认", r1)
        self.assertIsNotNone(bot._PENDING)
        r2 = bot.handle("确认", s, s.cfg)
        self.assertIn("已添加", r2)
        # 增量追加:原有主播一个都不能少
        self.assertEqual([x["anchor"] for x in s.cfg["monitors"]],
                         ["testanchor1", "li777cm"],
                         "必须增量追加,原有主播绝不能丢(误删 9 位主播的前科)")
        self.assertEqual(s.cfg["monitors"][1]["auto_record"], True)
        self.assertIsNone(bot._PENDING, "执行后确认态要清掉")

    def test_cancel_flow(self):
        bot._PENDING = None
        s = FakeState()
        bot.handle("添加主播 newstar01", s, s.cfg)
        r = bot.handle("取消", s, s.cfg)
        self.assertIn("取消", r)
        self.assertIsNone(bot._PENDING)
        self.assertEqual(len(s.cfg["monitors"]), 1, "取消后不得写入")

    def test_pending_expires(self):
        bot._PENDING = None
        s = FakeState()
        bot.handle("添加主播 li777cm", s, s.cfg)
        bot._PENDING["expires"] = time.time() - 1     # 人为过期
        r = bot.handle("确认", s, s.cfg)
        self.assertNotIn("已添加", r, "过期确认不得执行")
        self.assertEqual(len(s.cfg["monitors"]), 1)

    def test_update_failure_reported(self):
        bot._PENDING = None
        s = FakeState()

        def fail_update(patch):
            return {"ok": False, "error": "磁盘 IO 错误"}

        s.update_config = fail_update
        s.cfg["monitors"] = [{"name": "主播甲", "anchor": "testanchor1"}]
        bot.handle("添加主播 li777cm", s, s.cfg)
        r = bot.handle("确认", s, s.cfg)
        self.assertIn("❌", r, "写入失败必须把原因告诉用户,不能假报成功")


class TestExtract(unittest.TestCase):
    """消息结构未定稿:多候选键容错,任何一种常见结构都要能提取。"""

    def test_plain_id_text(self):
        mid, text, sender = bot._extract({"id": "m1", "text": "录制中", "from": "u1"})
        self.assertEqual((mid, text, sender), ("m1", "录制中", "u1"))

    def test_nested_text_dict(self):
        mid, text, _ = bot._extract({"msg_id": "m2", "text": {"content": "菜单"},
                                     "sender": {"id": "u2"}})
        self.assertEqual((mid, text), ("m2", "菜单"))

    def test_content_key(self):
        mid, text, _ = bot._extract({"message_id": "m3", "content": "状态"})
        self.assertEqual((mid, text), ("m3", "状态"))

    def test_unparseable_returns_empty_text(self):
        mid, text, _ = bot._extract({"id": "m4", "payload": 42})
        self.assertEqual((mid, text), ("m4", ""))


class TestDedup(unittest.TestCase):
    def test_seen_save_load_roundtrip(self):
        bot._SEEN = {"a", "b"}
        bot._save_seen()
        bot._SEEN = set()
        bot._load_seen()
        self.assertEqual(bot._SEEN, {"a", "b"})
        bot._SEEN = set()
        bot._save_seen()


if __name__ == "__main__":
    unittest.main()
