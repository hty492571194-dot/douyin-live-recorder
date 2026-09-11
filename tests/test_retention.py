# -*- coding: utf-8 -*-
"""孤儿主播目录延迟清理的单元测试。

全部在临时目录里跑,不碰真实的 previews/ recordings/ 与台账。
覆盖:登记/撤销/剪枝、幂等、冷静期、归档护栏、路径白名单、dry-run、状态视图。
"""

import json
import os
import shutil
import tempfile
import time
import unittest

import retention


class _Base(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="rt-")
        self.ledger = os.path.join(self.root, "spool", "retention.json")
        os.makedirs(os.path.join(self.root, "previews"), exist_ok=True)
        os.makedirs(os.path.join(self.root, "recordings"), exist_ok=True)

    def tearDown(self):
        shutil.rmtree(self.root, ignore_errors=True)

    def _cfg(self, *names):
        return {"monitors": [{"name": n} for n in names]}

    def _mkdir(self, rel):
        p = os.path.join(self.root, rel)
        os.makedirs(p, exist_ok=True)
        with open(os.path.join(p, "x.txt"), "w") as f:
            f.write("x")
        return p


class TestMark(_Base):
    def test_orphan_marked(self):
        self._mkdir("previews/甲")
        r = retention.mark(self._cfg(), path=self.ledger, root=self.root)
        self.assertEqual(r["added"], ["甲"])
        data = retention._load(self.ledger)
        self.assertIn("甲", data["items"])
        self.assertAlmostEqual(data["items"]["甲"]["marked_at"], time.time(), delta=5)

    def test_both_roots_recorded(self):
        self._mkdir("previews/甲")
        self._mkdir("recordings/甲")
        retention.mark(self._cfg(), path=self.ledger, root=self.root)
        it = retention._load(self.ledger)["items"]["甲"]
        self.assertEqual(sorted(it["dirs"]), ["previews/甲", "recordings/甲"])

    def test_idempotent_no_refresh(self):
        """重复 mark 不能刷新 marked_at —— 否则永远删不掉。"""
        self._mkdir("previews/甲")
        t0 = time.time() - 1000
        retention.mark(self._cfg(), now=t0, path=self.ledger, root=self.root)
        first = retention._load(self.ledger)["items"]["甲"]["marked_at"]
        retention.mark(self._cfg(), now=time.time(), path=self.ledger, root=self.root)
        second = retention._load(self.ledger)["items"]["甲"]["marked_at"]
        self.assertEqual(first, second)

    def test_re_added_revokes(self):
        self._mkdir("previews/甲")
        retention.mark(self._cfg(), path=self.ledger, root=self.root)
        r = retention.mark(self._cfg("甲"), path=self.ledger, root=self.root)
        self.assertEqual(r["revoked"], ["甲"])
        self.assertEqual(retention._load(self.ledger)["items"], {})

    def test_vanished_dir_pruned(self):
        self._mkdir("previews/甲")
        retention.mark(self._cfg(), path=self.ledger, root=self.root)
        shutil.rmtree(os.path.join(self.root, "previews", "甲"))
        r = retention.mark(self._cfg(), path=self.ledger, root=self.root)
        self.assertEqual(r["pruned"], ["甲"])
        self.assertEqual(retention._load(self.ledger)["items"], {})

    def test_active_streamer_not_marked(self):
        self._mkdir("previews/甲")
        r = retention.mark(self._cfg("甲"), path=self.ledger, root=self.root)
        self.assertEqual(r["added"], [])
        self.assertEqual(retention._load(self.ledger)["items"], {})


class TestSweep(_Base):
    def setUp(self):
        super().setUp()
        self.t0 = time.time()

    def _marked(self, name, dirs, age_days=31):
        retention._save({"version": 1, "items": {
            name: {"marked_at": self.t0 - age_days * 86400, "dirs": dirs}}}, self.ledger)

    def test_before_grace_is_pending(self):
        self._mkdir("previews/甲")
        retention.mark(self._cfg(), path=self.ledger, root=self.root)
        r = retention.sweep(self._cfg(), [], apply=True, path=self.ledger, root=self.root)
        self.assertEqual(r["deleted"], [])
        self.assertTrue(os.path.isdir(os.path.join(self.root, "previews", "甲")))
        self.assertEqual(len(r["pending"]), 1)

    def test_previews_deleted_after_grace(self):
        """缩略图可重建,不需要归档护栏。"""
        self._mkdir("previews/甲")
        self._marked("甲", ["previews/甲"])
        r = retention.sweep(self._cfg(), [], apply=True, path=self.ledger, root=self.root)
        self.assertEqual(r["deleted"], ["previews/甲"])
        self.assertFalse(os.path.exists(os.path.join(self.root, "previews", "甲")))
        self.assertTrue(r["applied"])

    def test_recordings_requires_archived(self):
        self._mkdir("recordings/甲")
        self._marked("甲", ["recordings/甲"])
        hist = [{"name": "甲", "archive": {"state": "archived"}}]
        r = retention.sweep(self._cfg(), hist, apply=True, path=self.ledger, root=self.root)
        self.assertEqual(r["deleted"], ["recordings/甲"])

    def test_recordings_blocked_by_none(self):
        self._mkdir("recordings/甲")
        self._marked("甲", ["recordings/甲"])
        hist = [{"name": "甲", "archive": {"state": "none"}}]
        r = retention.sweep(self._cfg(), hist, apply=True, path=self.ledger, root=self.root)
        self.assertEqual(r["deleted"], [])
        self.assertTrue(os.path.isdir(os.path.join(self.root, "recordings", "甲")))
        self.assertIn("none", r["skipped"][0]["reason"])

    def test_recordings_blocked_by_missing(self):
        self._mkdir("recordings/甲")
        self._marked("甲", ["recordings/甲"])
        hist = [{"name": "甲", "archive": {"state": "missing"}}]
        r = retention.sweep(self._cfg(), hist, apply=True, path=self.ledger, root=self.root)
        self.assertEqual(r["deleted"], [])

    def test_allow_missing_releases(self):
        self._mkdir("recordings/甲")
        self._marked("甲", ["recordings/甲"])
        hist = [{"name": "甲", "archive": {"state": "missing"}}]
        r = retention.sweep(self._cfg(), hist, apply=True, allow_missing=True,
                            path=self.ledger, root=self.root)
        self.assertEqual(r["deleted"], ["recordings/甲"])

    def test_re_added_streamer_skipped(self):
        self._mkdir("previews/甲")
        self._marked("甲", ["previews/甲"])
        r = retention.sweep(self._cfg("甲"), [], apply=True, path=self.ledger, root=self.root)
        self.assertEqual(r["deleted"], [])
        self.assertTrue(os.path.isdir(os.path.join(self.root, "previews", "甲")))

    def test_dry_run_does_not_delete(self):
        self._mkdir("previews/甲")
        self._marked("甲", ["previews/甲"])
        r = retention.sweep(self._cfg(), [], apply=False, path=self.ledger, root=self.root)
        self.assertEqual(r["deleted"], ["previews/甲"])   # 报告「将会删除」
        self.assertTrue(os.path.isdir(os.path.join(self.root, "previews", "甲")))
        self.assertFalse(r["applied"])

    def test_idempotent_second_sweep(self):
        self._mkdir("previews/甲")
        self._marked("甲", ["previews/甲"])
        retention.sweep(self._cfg(), [], apply=True, path=self.ledger, root=self.root)
        r = retention.sweep(self._cfg(), [], apply=True, path=self.ledger, root=self.root)
        self.assertEqual(r["deleted"], [])               # 已删,第二次无动作
        self.assertEqual(retention._load(self.ledger)["items"], {})

    def test_unsafe_path_rejected(self):
        """台账里被塞进项目外的路径时,必须拒绝(NAS/挂载点保护)。"""
        outside = tempfile.mkdtemp(prefix="rt-out-")
        try:
            self._marked("甲", [outside])
            r = retention.sweep(self._cfg(), [], apply=True, path=self.ledger, root=self.root)
            self.assertEqual(r["deleted"], [])
            self.assertTrue(os.path.isdir(outside))
        finally:
            shutil.rmtree(outside, ignore_errors=True)

    def test_unsafe_name_rejected(self):
        self._marked("../../etc", ["previews/x"])
        r = retention.sweep(self._cfg(), [], apply=True, path=self.ledger, root=self.root)
        self.assertEqual(r["deleted"], [])
        self.assertIn("不合法", r["skipped"][0]["reason"])


class TestSafeDirCheck(_Base):
    def test_accepts_normal_dir(self):
        self.assertTrue(retention._is_local_safe_dir(
            os.path.join(self.root, "previews", "甲"), self.root))

    def test_rejects_deeper_path(self):
        p = os.path.join(self.root, "previews", "甲", "sub")
        os.makedirs(p, exist_ok=True)
        self.assertFalse(retention._is_local_safe_dir(p, self.root))

    def test_rejects_unwatched_root(self):
        p = os.path.join(self.root, "auth", "甲")
        os.makedirs(p, exist_ok=True)
        self.assertFalse(retention._is_local_safe_dir(p, self.root))

    def test_rejects_symlink(self):
        """软链到 NAS 的目录必须被挡住 —— 已迁移到 NAS 的一律跳过。"""
        target = tempfile.mkdtemp(prefix="rt-nas-")
        link = os.path.join(self.root, "recordings", "甲")
        try:
            os.symlink(target, link)
            self.assertFalse(retention._is_local_safe_dir(link, self.root))
        finally:
            shutil.rmtree(target, ignore_errors=True)


class TestStatus(_Base):
    def test_days_left_and_ready(self):
        self._mkdir("previews/甲")
        retention.mark(self._cfg(), now=time.time() - 10 * 86400,
                       path=self.ledger, root=self.root)
        st = retention.status(self._cfg(), [], path=self.ledger, root=self.root)
        self.assertEqual(len(st["items"]), 1)
        it = st["items"][0]
        self.assertEqual(it["name"], "甲")
        self.assertEqual(it["age_days"], 10)
        self.assertEqual(it["days_left"], 20)
        self.assertFalse(it["ready"])
        self.assertFalse(it["archive"]["blocked"])


class TestRunDue(_Base):
    def test_mark_and_sweep_combined(self):
        self._mkdir("previews/甲")
        r = retention.run_due(self._cfg(), [], path=self.ledger, root=self.root)
        self.assertEqual(r["mark"]["added"], ["甲"])
        self.assertEqual(r["sweep"]["deleted"], [])      # 未满 30 天


if __name__ == "__main__":
    unittest.main()
