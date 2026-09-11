#!/usr/bin/env python3
"""历史归档状态 / 文件位置 / 重新定位 的单元测试(临时目录,不碰真实数据)。"""
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import monitor  # noqa: E402
import webui  # noqa: E402

DAY = "2026-08-29"
STEM = "主播A-20260829-120000"


class TestHistoryArchive(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.old_hist = monitor.HISTORY_PATH
        monitor.HISTORY_PATH = os.path.join(self.base, "history.json")
        monitor._history = []
        self.rec = os.path.join(self.base, "recordings")
        self.nas = os.path.join(self.base, "nas")
        self.ext = os.path.join(self.base, "ext")
        for d in (self.rec, self.nas, self.ext):
            os.makedirs(d, exist_ok=True)
        self.cfg = {
            "recorder": {"output_dir": self.rec},
            "nas": {"enabled": True, "mount_point": self.nas,
                    "root_dir": "直播回放", "share": "s", "host": "h"},
            "archive": {"mode": "auto", "external_dir": self.ext},
        }

    def tearDown(self):
        monitor.HISTORY_PATH = self.old_hist
        monitor._history = []
        self.tmp.cleanup()

    def _entry(self, archived=False, dest=None, state="none"):
        return {
            "name": "主播A", "status": "done", "started_at": time.time(),
            "ended_at": time.time(), "duration": 100, "size": 1024,
            "output_path": os.path.join(self.rec, "主播A", DAY, STEM + "-%03d.flv"),
            "archive": {"state": state, "target": None, "dest": dest,
                        "at": None, "error": None},
        }

    # ── 1. 归档状态回写 ──
    def test_history_add_has_archive_field(self):
        monitor._history_add(None, "主播A",
                             os.path.join(self.rec, "主播A", DAY, STEM + "-%03d.flv"),
                             time.time())
        self.assertEqual(monitor._history[0]["archive"]["state"], "none")

    def test_mark_pending_then_archived(self):
        e = self._entry()
        monitor._history = [e]
        session = os.path.dirname(e["output_path"])
        monitor._mark_archive_state(session, "pending")
        self.assertEqual(e["archive"]["state"], "pending")
        dest = os.path.join(self.nas, "直播回放", "主播A", DAY)
        monitor._mark_archive_state(session, "archived", dest=dest, target="nas")
        self.assertEqual(e["archive"]["state"], "archived")
        self.assertEqual(e["archive"]["dest"], dest)

    def test_archived_not_downgraded(self):
        """已归档不被后续的 pending/archiving 覆盖(防重入队导致状态回退)。"""
        e = self._entry(state="archived", dest="/nas/x")
        monitor._history = [e]
        session = os.path.dirname(e["output_path"])
        monitor._mark_archive_state(session, "pending")
        self.assertEqual(e["archive"]["state"], "archived")

    def test_mark_failed_keeps_error(self):
        e = self._entry()
        monitor._history = [e]
        session = os.path.dirname(e["output_path"])
        monitor._mark_archive_state(session, "failed", error="磁盘已满")
        self.assertEqual(e["archive"]["state"], "failed")
        self.assertEqual(e["archive"]["error"], "磁盘已满")

    # ── 2. 文件位置计算(本机 / 归档自动切换)──
    def test_files_local(self):
        f = webui._history_files(self._entry(), self.cfg)
        self.assertEqual(f["location"], "local")
        self.assertTrue(f["video"].endswith(STEM + "-%03d.flv"))
        self.assertTrue(f["meta_dir"].endswith(os.path.join(DAY, ".meta")))
        self.assertTrue(f["jsonl"].endswith(STEM + ".danmaku.jsonl"))
        self.assertTrue(f["align"].endswith(STEM + ".align.json"))
        # 校验文件与视频在同一场次目录下(要求 3)
        self.assertTrue(f["align"].startswith(os.path.dirname(f["video"])))

    def test_files_archived_switch_to_dest(self):
        dest = os.path.join(self.nas, "直播回放", "主播A", DAY)
        f = webui._history_files(self._entry(state="archived", dest=dest), self.cfg)
        self.assertEqual(f["location"], "archive")
        self.assertTrue(f["video"].startswith(dest))
        self.assertTrue(f["align"].startswith(dest))
        self.assertFalse(f["video"].startswith(self.rec))  # 不再指向已删的本机路径

    # ── 3. 重新定位(存储位置变化后自动更新链接)──
    def test_relocate_finds_archived_copy(self):
        """本机目录已不在,NAS 上有对应 <主播>/<日期> → 自动重新定位。"""
        dest = os.path.join(self.nas, "直播回放", "主播A", DAY)
        os.makedirs(dest, exist_ok=True)
        e = self._entry()
        monitor._history = [e]
        n = monitor._relocate_history(self.cfg)
        self.assertEqual(n, 1)
        self.assertEqual(e["archive"]["state"], "archived")
        self.assertEqual(e["archive"]["dest"], dest)

    def test_relocate_skips_existing_local(self):
        """本机视频还在 → 不重定位。"""
        d = os.path.dirname(self._entry()["output_path"])
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, STEM + "-000.flv"), "w", encoding="utf-8") as f:
            f.write("fake")          # glob 要能匹配到真实视频文件
        e = self._entry()
        monitor._history = [e]
        self.assertEqual(monitor._relocate_history(self.cfg), 0)
        self.assertEqual(e["archive"]["state"], "none")

    def test_relocate_marks_missing(self):
        """本机没有、归档目标也没有 → 标记 missing(不再显示本机死路径)。"""
        e = self._entry()
        monitor._history = [e]
        self.assertEqual(monitor._relocate_history(self.cfg), 0)
        self.assertEqual(e["archive"]["state"], "missing")
        self.assertIn("未找到", e["archive"]["error"] or "")

    def test_relocate_external_disk(self):
        """NAS 上没有、外置硬盘上有 → 定位到外置硬盘。"""
        dest = os.path.join(self.ext, "直播回放", "主播A", DAY)
        os.makedirs(dest, exist_ok=True)
        e = self._entry()
        monitor._history = [e]
        self.assertEqual(monitor._relocate_history(self.cfg), 1)
        self.assertEqual(e["archive"]["target"], "external")

    # ── 4. 打开白名单(多环境适配,防任意路径)──
    def test_allowed_roots_three_envs(self):
        roots = webui._allowed_roots(self.cfg)
        self.assertIn(os.path.realpath(self.rec), roots)
        self.assertIn(os.path.realpath(self.nas), roots)
        self.assertIn(os.path.realpath(self.ext), roots)
        self.assertEqual(len(roots), 3)

    def test_allowed_roots_excludes_others(self):
        roots = webui._allowed_roots(self.cfg)
        self.assertNotIn(os.path.realpath("/Users"), roots)


if __name__ == "__main__":
    unittest.main()
