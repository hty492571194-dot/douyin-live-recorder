#!/usr/bin/env python3
"""目录列举缓存 / 历史接口缓存 的单元测试(临时目录,不碰真实数据)。

覆盖 2026-10-26「添加主播的解析按钮永久停在解析中…」的修复:

/api/history 曾对 500 条历史逐条做 6 次 os.path.exists 判存在性,而 493 条是
归档条目、全在 NAS 上,每条又指向不同日期目录。实测单次请求 43 秒,前端却每
5 秒轮询一次且没有并发守卫 —— 请求无限堆积占满浏览器对同一 origin 的 6 条
连接,别的接口(包括 /api/resolve)连 TCP 都建不起来,而服务端其实是空闲的。

修复分两层:
  后端 _dir_listing  —— 同一目录只 scandir 一次,内存判成员(2946 次 SMB → 78 次)
  后端 _history_payload —— 结果 TTL 缓存 + 单飞
  前端 pollJSON      —— 单飞守卫 + 超时(见 static/index.html 顶部说明)

这里重点守住三态语义:True=在 / False=确实没有 / None=无法确认。前端对
False 显示「✗ 缺失」、对 None 显示「? 无法确认」,两者绝不能混 —— 混了就会
把已删除的归档显示成「未挂载」,或反过来让人以为文件被删了。
"""
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import webui  # noqa: E402

DAY = "2026-08-29"
STEM = "主播A-20260829-120000"


class TestDirListing(unittest.TestCase):
    """_dir_listing 的三态与缓存。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        self.old_ttl = webui._DIR_TTL
        webui._dir_cache_clear()

    def tearDown(self):
        webui._DIR_TTL = self.old_ttl
        webui._dir_cache_clear()
        self.tmp.cleanup()

    def test_ok_returns_names(self):
        with open(os.path.join(self.dir, "a.flv"), "w"):
            pass
        os.makedirs(os.path.join(self.dir, ".meta"))
        state, names = webui._dir_listing(self.dir)
        self.assertEqual(state, webui._DIR_OK)
        self.assertIn("a.flv", names)
        self.assertIn(".meta", names)

    def test_missing_has_its_own_state(self):
        """目录确实不存在 → MISSING。不能退化成 UNKNOWN,否则前端会把
        「归档已被清理」显示成「归档目标未挂载」。"""
        state, names = webui._dir_listing(os.path.join(self.dir, "没有这个目录"))
        self.assertEqual(state, webui._DIR_MISSING)
        self.assertEqual(names, frozenset())

    def test_path_is_file_is_missing(self):
        """传进来的是一条文件路径(NotADirectoryError)也算 MISSING。"""
        p = os.path.join(self.dir, "x.flv")
        with open(p, "w"):
            pass
        state, _ = webui._dir_listing(p)
        self.assertEqual(state, webui._DIR_MISSING)

    def test_empty_path_is_unknown(self):
        self.assertEqual(webui._dir_listing("")[0], webui._DIR_UNKNOWN)
        self.assertEqual(webui._dir_listing(None)[0], webui._DIR_UNKNOWN)

    def test_permission_error_is_unknown(self):
        """读不了 ≠ 不存在。权限错误必须报 UNKNOWN,不能报 MISSING。"""
        d = os.path.join(self.dir, "locked")
        os.makedirs(d)
        os.chmod(d, 0o000)
        try:
            if os.access(d, os.R_OK):
                self.skipTest("当前用户不受目录权限限制(如 root)")
            self.assertEqual(webui._dir_listing(d)[0], webui._DIR_UNKNOWN)
        finally:
            os.chmod(d, 0o755)

    def test_ttl_hit_then_expiry(self):
        p = os.path.join(self.dir, "c")
        os.makedirs(p)
        self.assertEqual(webui._dir_listing(p)[0], webui._DIR_OK)
        # TTL 内应直接复用快照:新文件不立刻可见,这是「5 秒轮询不重复打 NAS」
        # 的前提,也是归档状态最多滞后一个 TTL 的代价。
        with open(os.path.join(p, "new.flv"), "w"):
            pass
        self.assertNotIn("new.flv", webui._dir_listing(p)[1])
        webui._DIR_TTL = 0
        self.assertIn("new.flv", webui._dir_listing(p)[1])

    def test_cache_clear_forces_relist(self):
        p = os.path.join(self.dir, "c2")
        os.makedirs(p)
        webui._dir_listing(p)
        with open(os.path.join(p, "n.flv"), "w"):
            pass
        webui._dir_cache_clear()
        self.assertIn("n.flv", webui._dir_listing(p)[1])

    def test_missing_is_cached_too(self):
        """不存在的目录也要缓存:否则每次轮询都去打一趟 NAS 拿 NOT_FOUND。"""
        p = os.path.join(self.dir, "nothere")
        webui._dir_listing(p)
        self.assertIn(os.path.abspath(p), webui._DIR_CACHE)


class TestHistoryFilesThreeState(unittest.TestCase):
    """_history_files 的三态映射与 root 推导一致性。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.rec = os.path.join(self.base, "recordings")
        self.nas = os.path.join(self.base, "nas")
        os.makedirs(self.rec, exist_ok=True)
        os.makedirs(self.nas, exist_ok=True)
        self.old_ttl = webui._DIR_TTL
        webui._dir_cache_clear()
        self.cfg = {
            "recorder": {"output_dir": self.rec},
            "nas": {"enabled": True, "mount_point": self.nas,
                    "root_dir": "直播回放", "share": "share_video",
                    "host": "192.168.1.10"},
            "archive": {"mode": "auto", "external_dir": ""},
        }

    def tearDown(self):
        webui._DIR_TTL = self.old_ttl
        webui._dir_cache_clear()
        self.tmp.cleanup()

    def _entry(self, state="none", dest=None):
        return {
            "name": "主播A", "status": "done", "started_at": time.time(),
            "ended_at": time.time(), "duration": 100, "size": 1024,
            "output_path": os.path.join(self.rec, "主播A", DAY,
                                        STEM + "-%03d.flv"),
            "archive": {"state": state, "target": None, "dest": dest,
                        "at": None, "error": None},
        }

    def _local_day(self):
        day = os.path.join(self.rec, "主播A", DAY)
        os.makedirs(os.path.join(day, ".meta"), exist_ok=True)
        return day

    def test_true_and_false_are_distinguished(self):
        day = self._local_day()
        with open(os.path.join(day, STEM + "-000.ass"), "w"):
            pass
        with open(os.path.join(day, ".meta", STEM + ".danmaku.jsonl"), "w"):
            pass
        f = webui._history_files(self._entry(), self.cfg)
        self.assertIs(f["ass_exists"], True)
        self.assertIs(f["jsonl_exists"], True)
        # align 确实没有 → False(显示 ✗ 缺失),不能是 None(显示 ? 无法确认)
        self.assertIs(f["align_exists"], False)
        self.assertIs(f["dir_exists"], True)
        self.assertIs(f["video_exists"], True)

    def test_missing_day_dir_reports_false(self):
        f = webui._history_files(self._entry(), self.cfg)   # 一天都没建
        self.assertIs(f["dir_exists"], False)
        self.assertIs(f["ass_exists"], False)
        self.assertIs(f["jsonl_exists"], False)
        self.assertIs(f["align_exists"], False)

    def test_unmounted_nas_reports_unknown(self):
        """归档目标不可达 → 全部 None(前端「无法确认」),绝不能报 False。"""
        dest = os.path.join(self.nas, "直播回放", "主播A", DAY)
        cfg = json.loads(json.dumps(self.cfg))
        cfg["nas"] = dict(cfg["nas"],
                          mount_point=os.path.join(self.base, "不存在的挂载点"))
        f = webui._history_files(self._entry(state="archived", dest=dest), cfg)
        self.assertEqual(f["location"], "archive")
        self.assertIsNone(f["dir_exists"])
        self.assertIsNone(f["ass_exists"])
        self.assertIsNone(f["jsonl_exists"])
        self.assertIsNone(f["align_exists"])

    def test_archived_reads_from_dest(self):
        dest = os.path.join(self.nas, "直播回放", "主播A", DAY)
        os.makedirs(os.path.join(dest, ".meta"))
        with open(os.path.join(dest, ".meta", STEM + ".align.json"), "w"):
            pass
        f = webui._history_files(self._entry(state="archived", dest=dest),
                                self.cfg)
        self.assertTrue(f["video"].startswith(dest))
        self.assertIs(f["align_exists"], True)
        self.assertIs(f["jsonl_exists"], False)

    def test_entry_root_matches_history_files(self):
        """_entry_root 与 _history_files 的 root 必须是同一个推导 ——
        否则 _prefetch_paths 预热的目录和实际探测的目录对不上,缓存形同虚设。"""
        dest = os.path.join(self.nas, "直播回放", "主播A", DAY)
        for state, d in (("none", None), ("archived", dest)):
            e = self._entry(state=state, dest=d)
            self.assertEqual(webui._entry_root(e),
                             webui._history_files(e, self.cfg)["dir"])

    def test_has_entry_file(self):
        day = self._local_day()
        with open(os.path.join(day, ".meta", "x.jsonl"), "w"):
            pass
        self.assertTrue(webui._has_entry_file(day, ".meta", "x.jsonl"))
        self.assertFalse(webui._has_entry_file(day, ".meta", "y.jsonl"))
        self.assertFalse(webui._has_entry_file(day, "没有子目录", "x.jsonl"))


class TestHistoryPayloadCache(unittest.TestCase):
    """/api/history 的 TTL 缓存 + 单飞。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.rec = self.tmp.name
        os.makedirs(os.path.join(self.rec, "主播A", DAY), exist_ok=True)
        self.old_hist_ttl = webui._HIST_TTL
        self.old_dir_ttl = webui._DIR_TTL
        webui._hist_cache_clear()
        webui._dir_cache_clear()
        self.calls = 0
        self.cfg = {
            "recorder": {"output_dir": self.rec},
            "nas": {"enabled": False, "mount_point": "", "root_dir": "",
                    "share": "", "host": ""},
            "archive": {"mode": "auto", "external_dir": ""},
        }
        self.hist = [{
            "name": "主播A", "status": "done", "started_at": time.time(),
            "ended_at": time.time(), "duration": 100, "size": 1024,
            "output_path": os.path.join(self.rec, "主播A", DAY,
                                        STEM + "-%03d.flv"),
            "archive": {"state": "none", "dest": None},
        }]

    def tearDown(self):
        webui._HIST_TTL = self.old_hist_ttl
        webui._DIR_TTL = self.old_dir_ttl
        webui._hist_cache_clear()
        webui._dir_cache_clear()
        self.tmp.cleanup()

    def _state(self):
        outer = self

        class _St:
            def get_config(self):
                return outer.cfg

            def get_history(self):
                outer.calls += 1        # 深拷贝是真实开销:命中缓存时不该发生
                return json.loads(json.dumps(outer.hist))
        return _St()

    def test_payload_shape(self):
        p = webui._history_payload(self._state())
        self.assertEqual(len(p["entries"]), 1)
        self.assertIn("files", p["entries"][0])
        self.assertIn("nas_mounted", p)
        self.assertIn("archive_roots", p)

    def test_cache_hit_skips_recompute(self):
        st = self._state()
        p1 = webui._history_payload(st)
        p2 = webui._history_payload(st)
        self.assertIs(p1, p2)            # 命中缓存返回同一对象
        self.assertEqual(self.calls, 1)  # get_history 只调一次

    def test_ttl_zero_recomputes_every_time(self):
        webui._HIST_TTL = 0
        st = self._state()
        webui._history_payload(st)
        webui._history_payload(st)
        self.assertEqual(self.calls, 2)

    def test_clear_forces_recompute(self):
        st = self._state()
        webui._history_payload(st)
        webui._hist_cache_clear()
        webui._history_payload(st)
        self.assertEqual(self.calls, 2)


if __name__ == "__main__":
    unittest.main()
