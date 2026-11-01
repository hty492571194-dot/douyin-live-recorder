#!/usr/bin/env python3
"""nas.py 单元测试(DRY_RUN + 临时目录,不依赖真实 NAS)。"""
import json
import os
import subprocess
import tempfile
import time
import unittest

import nas


class TestNas(unittest.TestCase):
    def setUp(self):
        nas.DRY_RUN = True
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        # 重定向 spool 与挂载点到临时目录
        nas.SPOOL_DIR = os.path.join(self.base, "spool")
        nas.PENDING_PATH = os.path.join(nas.SPOOL_DIR, "pending.json")
        self.mount_point = os.path.join(self.base, "mount")
        os.makedirs(self.mount_point, exist_ok=True)
        self.nas_cfg = {
            "enabled": True, "host": "h", "share": "s",
            "mount_point": self.mount_point, "root_dir": "录播",
        }
        nas._pending = []
        nas._last_archive = None

    def tearDown(self):
        nas.DRY_RUN = False
        self.tmp.cleanup()

    def _cfg(self):
        return {"nas": self.nas_cfg}

    def test_enqueue_and_archive_success(self):
        """入队 → 搬运成功 → 队列清空 + 目标落盘(带日期层) + 本地源删除。"""
        src_dir = os.path.join(self.base, "src")
        os.makedirs(src_dir)
        src = os.path.join(src_dir, "rec.mp4")
        with open(src, "w", encoding="utf-8") as f:
            f.write("fake-video-data")
        nas.enqueue({"streamer": "s1", "src_path": src, "room_id": "123",
                     "date": "2026-08-28"})
        self.assertEqual(nas.queue_status()["depth"], 1)

        nas.process_queue(self._cfg())

        qs = nas.queue_status()
        self.assertEqual(qs["depth"], 0)  # 队列清空
        # 新层级:<mount>/<root_dir>/<主播>/<YYYY-MM-DD>/<文件>
        dest = os.path.join(self.mount_point, "录播", "s1", "2026-08-28", "rec.mp4")
        self.assertTrue(os.path.exists(dest))
        self.assertTrue(os.path.exists(dest + ".meta.json"))
        self.assertFalse(os.path.exists(src))  # 本地源已删除
        self.assertIsNotNone(qs["last_archive"])

    def test_archive_date_dir_moves_content(self):
        """目录条目(场次日期目录):内容直接搬进 <主播>/<date>/,不再嵌套同名层。"""
        day_dir = os.path.join(self.base, "src", "s4", "2026-08-28")
        os.makedirs(day_dir)
        open(os.path.join(day_dir, "a.flv"), "w").write("v")
        os.makedirs(os.path.join(day_dir, ".meta"))
        open(os.path.join(day_dir, ".meta", "a.danmaku.jsonl"), "w").write("{}")
        nas.enqueue({"streamer": "s4", "src_path": day_dir, "date": "2026-08-28"})
        nas.process_queue(self._cfg())
        dest = os.path.join(self.mount_point, "录播", "s4", "2026-08-28")
        self.assertTrue(os.path.exists(os.path.join(dest, "a.flv")))
        self.assertTrue(os.path.exists(os.path.join(dest, ".meta", "a.danmaku.jsonl")))
        self.assertFalse(os.path.exists(os.path.join(dest, "2026-08-28")))  # 不嵌套
        self.assertFalse(os.path.exists(day_dir))  # 源已删除

    def test_archive_legacy_root_entry_blocked(self):
        """旧版错误条目(src=录制根目录)被拦截标 failed,不再误搬全部主播。"""
        root = os.path.join(self.base, "recordings")
        os.makedirs(root)
        open(os.path.join(root, "x.flv"), "w").write("v")
        nas.enqueue({"streamer": "s5", "src_path": root})
        nas.process_queue(self._cfg())
        self.assertEqual(nas._pending[0]["status"], "failed")
        self.assertFalse(os.path.exists(os.path.join(self.mount_point, "录播", "s5")))
        self.assertTrue(os.path.exists(root))  # 源未被误删

    def test_missing_source_fails_at_once(self):
        """源不存在 → **一次就判失败**,不再重试 5 次。

        旧行为是 retry ×5 然后 failed:这些「重试」纯属空转(源目录都已经没了),
        而且耗尽后会回调 `_notify_archive(failed)`,把本该是 archived 的 history
        状态覆写成「失败」—— 素材明明躺在目标盘上,历史页却报失败。见
        `test_archived_state_not_downgraded_by_stale_queue_entry`。
        """
        nas.enqueue({"streamer": "s2", "src_path": "/nonexistent/x.mp4"})
        nas.process_queue(self._cfg())
        qs = nas.queue_status()
        self.assertEqual(qs["failed"], 1)
        self.assertEqual(qs["depth"], 0)
        entry = nas._pending[0]
        self.assertEqual(entry["status"], "failed")
        self.assertEqual(entry.get("retries", 0), 0)     # 一次都没浪费
        self.assertIn("源已不存在", entry["error"])

    def test_missing_source_but_target_has_media_counts_done(self):
        """源已消失、但目标已有媒体文件 ⇒ 认作归档完成(顺手校正 history)。"""
        d = os.path.join(self.base, "recordings", "s9", "2026-09-01")
        os.makedirs(d)
        with open(os.path.join(d, "a.flv"), "w") as f:
            f.write("v" * 10)
        had = []
        nas.set_archive_hook(lambda src, st, dest=None, tgt=None, err=None:
                             had.append(st))
        try:
            nas.enqueue({"streamer": "s9", "src_path": d, "date": "2026-09-01"})
            nas.process_queue(self._cfg())
        finally:
            nas.set_archive_hook(None)
        self.assertEqual(nas.queue_status()["depth"], 0)          # 条目被消费掉
        self.assertNotIn("failed", had)
        self.assertIn("archived", had)

    def test_missing_source_and_empty_target_fails(self):
        """源没了、目标也没有 ⇒ 失败并说明原因(不能悄悄算成功)。"""
        nas.enqueue({"streamer": "s8", "src_path": "/nonexistent/y.flv"})
        nas.process_queue(self._cfg())
        e = nas._pending[0]
        self.assertEqual(e["status"], "failed")
        self.assertIn("目标无对应文件", e["error"])

    def test_target_dir_with_only_appledouble_is_not_media(self):
        """exFAT 的 `._xxx` 伴随文件不算媒体 —— 否则空壳目录会被当成功。"""
        d = os.path.join(self.base, "t")
        os.makedirs(d)
        for n in ("._a.flv", "a.meta.json", "readme.txt"):
            with open(os.path.join(d, n), "w", encoding="utf-8") as f:
                f.write("x")
        self.assertFalse(nas._dest_has_media(d))
        with open(os.path.join(d, "b.flv"), "w", encoding="utf-8") as f:
            f.write("x")
        self.assertTrue(nas._dest_has_media(d))
        self.assertFalse(nas._dest_has_media("/nonexistent/dir"))

    def test_pending_reload(self):
        """启动加载 pending.json 续传。"""
        item = [{"id": "abc", "streamer": "s3", "src_path": "/x",
                 "added_at": 1, "retries": 2, "status": "pending"}]
        os.makedirs(nas.SPOOL_DIR, exist_ok=True)
        with open(nas.PENDING_PATH, "w", encoding="utf-8") as f:
            json.dump(item, f)
        nas._pending = []
        nas._load_pending()
        self.assertEqual(len(nas._pending), 1)
        self.assertEqual(nas._pending[0]["streamer"], "s3")

    def test_test_connection_empty_host(self):
        """test 接口对空 host 返回明确错误;DRY_RUN 下非空 host 模拟成功。"""
        r = nas.test_connection({"host": "", "share": "x"})
        self.assertFalse(r["ok"])
        self.assertIn("error", r)
        r2 = nas.test_connection({"host": "h", "share": "s"})
        self.assertTrue(r2["ok"])

    def test_list_shares_empty_host(self):
        """list_shares 对空 host 返回错误;DRY_RUN 下返回模拟列表。"""
        r = nas.list_shares("")
        self.assertFalse(r[0])
        r2 = nas.list_shares("h", "u", "p")
        self.assertTrue(r2[0])
        self.assertIn("share1", r2[1])

    def test_parse_shares(self):
        """解析 smbutil view 输出,提取共享名列(列对齐由 header 的 Type 位置决定)。"""
        header = "Share                                           Type    Comments"
        type_col = header.find("Type")

        def dl(name):
            return name.ljust(type_col) + "Disk"

        out = "\n".join([
            header,
            "-" * 31,
            dl("share_video_公共空间"),
            dl("share_video"),
            dl("video"),
            "",
            "3 shares listed",
        ]) + "\n"
        shares = nas._parse_shares(out)
        self.assertEqual(shares, ["share_video_公共空间", "share_video", "video"])

    def test_clean_host_share(self):
        """主机/共享名规范化:剥离 smb:// 前缀与首尾斜杠。"""
        self.assertEqual(nas.clean_host("smb://192.168.1.10"), "192.168.1.10")
        self.assertEqual(nas.clean_host("smb://192.168.1.10/"), "192.168.1.10")
        self.assertEqual(nas.clean_host("cifs://nas.local"), "nas.local")
        self.assertEqual(nas.clean_host("192.168.1.10"), "192.168.1.10")
        self.assertEqual(nas.clean_share("/video/"), "video")
        self.assertEqual(nas.clean_share(" video "), "video")

    def test_smb_url_encoding(self):
        """URL 对用户名/密码/共享名 percent-encode,兼容特殊字符。"""
        u = nas._smb_url("192.168.1.10", "video", 445, "admin", "p@ss:word")
        self.assertEqual(u, "//admin:p%40ss%3Aword@192.168.1.10/video")
        self.assertEqual(nas._smb_url("h", "s", 445, "", ""), "//h/s")
        self.assertEqual(nas._smb_url("h", "s", 445, "admin", ""), "//admin@h/s")

    def test_resolve_target_auto_nas_first(self):
        """auto 模式 NAS 可用时优先 NAS(DRY_RUN 模拟挂载成功)。"""
        ok, target, mode = nas.resolve_target(
            {"nas": self.nas_cfg, "archive": {"mode": "auto", "external_dir": ""}})
        self.assertTrue(ok)
        self.assertEqual(mode, "nas")
        self.assertEqual(target, self.mount_point)

    def test_resolve_target_external(self):
        """external 模式:目录可写时返回外接目录。"""
        ext = os.path.join(self.base, "external")
        os.makedirs(ext)
        ok, target, mode = nas.resolve_target(
            {"nas": self.nas_cfg, "archive": {"mode": "external", "external_dir": ext}})
        self.assertTrue(ok)
        self.assertEqual(mode, "external")
        self.assertEqual(target, ext)

    def test_resolve_target_external_missing(self):
        """external 模式目录不存在 → 不可用。"""
        ok, target, mode = nas.resolve_target(
            {"nas": self.nas_cfg, "archive": {"mode": "external", "external_dir": "/nonexistent/x"}})
        self.assertFalse(ok)

    def test_resolve_target_auto_fallback_external(self):
        """auto 模式 NAS 未启用时降级外接硬盘。"""
        ext = os.path.join(self.base, "external")
        os.makedirs(ext)
        cfg = {"nas": {**self.nas_cfg, "enabled": False},
               "archive": {"mode": "auto", "external_dir": ext}}
        ok, target, mode = nas.resolve_target(cfg)
        self.assertTrue(ok)
        self.assertEqual(mode, "external")
        self.assertEqual(target, ext)

    def test_resolve_target_external_skips_nas_mount(self):
        """仅外接硬盘模式:即使 nas.enabled 仍为 true,也不碰 ensure_mount。

        回归点:历史实现在判断 mode 之前就无条件 ensure_mount(),NAS 不可达时
        会触发 auto_recover_host 全网段扫描(10~30s),把归档轮询白白拖慢。
        """
        ext = os.path.join(self.base, "external")
        os.makedirs(ext)
        cfg = {"nas": self.nas_cfg,
               "archive": {"mode": "external", "external_dir": ext}}
        calls = []
        orig = nas.ensure_mount
        nas.ensure_mount = lambda c: calls.append(c) or True
        try:
            ok, target, mode = nas.resolve_target(cfg)
        finally:
            nas.ensure_mount = orig
        self.assertTrue(ok)
        self.assertEqual(mode, "external")
        self.assertEqual(target, ext)
        self.assertEqual(calls, [], "external 模式不应调用 ensure_mount")

    def test_resolve_target_nas_mode_still_mounts(self):
        """对照:nas 模式与 auto 模式仍然会 ensure_mount(别把修复做过头)。"""
        orig = nas.ensure_mount
        for mode in ("nas", "auto"):
            calls = []
            nas.ensure_mount = lambda c: calls.append(c) or True
            try:
                nas.resolve_target({"nas": self.nas_cfg,
                                    "archive": {"mode": mode, "external_dir": ""}})
            finally:
                nas.ensure_mount = orig
            self.assertEqual(len(calls), 1, "mode=%s 应调用 ensure_mount" % mode)

    def test_resolve_target_backward_compat(self):
        """无 archive 键的旧 config:等价纯 NAS 行为。"""
        ok, target, mode = nas.resolve_target({"nas": self.nas_cfg})
        self.assertTrue(ok)
        self.assertEqual(mode, "nas")
        self.assertEqual(target, self.mount_point)

    def test_host_alive(self):
        """TCP 探测:不可达端口 False,本机临时监听端口 True。"""
        self.assertFalse(nas._host_alive("127.0.0.1", port=1, timeout=0.5))
        import socket as _s
        srv = _s.socket()
        srv.bind(("127.0.0.1", 0))
        srv.listen(1)
        port = srv.getsockname()[1]
        self.assertTrue(nas._host_alive("127.0.0.1", port=port, timeout=1))
        srv.close()

    def test_persist_host(self):
        """发现新 IP 后原子写回 config.json;同值幂等不写。"""
        import json as _json
        tmp_cfg = os.path.join(self.base, "config.json")
        with open(tmp_cfg, "w", encoding="utf-8") as f:
            _json.dump({"nas": {"host": "10.0.0.99", "share": "s"}}, f)
        old_path = nas.CONFIG_PATH
        nas.CONFIG_PATH = tmp_cfg
        try:
            self.assertTrue(nas.persist_host("10.0.0.2"))
            cfg = _json.load(open(tmp_cfg))
            self.assertEqual(cfg["nas"]["host"], "10.0.0.2")
            self.assertEqual(cfg["nas"]["share"], "s")  # 其余字段不动
            self.assertTrue(nas.persist_host("10.0.0.2"))  # 幂等
        finally:
            nas.CONFIG_PATH = old_path

    def test_discover_skips_unreachable(self):
        """网段内无任何 SMB 主机时 discover 返回 None 且不抛异常。"""
        orig = nas._local_net_prefix
        nas._local_net_prefix = lambda: "203.0.113"  # TEST-NET 网段,必然全不可达
        try:
            self.assertIsNone(nas.discover_nas_host(
                {"nas": {"host": "203.0.113.1", "share": "s",
                         "username": "", "port": 445}}))
        finally:
            nas._local_net_prefix = orig


class TestBackfillFromHistory(unittest.TestCase):
    """补归档:把历史里漏搬的场次重新入队。核心是不可误搬整个主播目录。"""
    def setUp(self):
        nas.DRY_RUN = True
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        nas.SPOOL_DIR = os.path.join(self.base, "spool")
        nas.PENDING_PATH = os.path.join(nas.SPOOL_DIR, "pending.json")
        nas._pending = []
        nas._last_archive = None
        self.hist = os.path.join(self.base, "history.json")

    def tearDown(self):
        nas.DRY_RUN = False
        self.tmp.cleanup()

    def _write_history(self, rows):
        with open(self.hist, "w", encoding="utf-8") as f:
            json.dump(rows, f)

    def _entry(self, name, rel_path, state="none"):
        return {"name": name, "status": "done", "started_at": 1787863726.0,
                "ended_at": 1787871286.0,
                "output_path": os.path.join(self.base, rel_path),
                "archive": {"state": state}}

    def _touch(self, rel_dir, fname="a.flv"):
        d = os.path.join(self.base, rel_dir)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, fname), "w", encoding="utf-8") as f:
            f.write("x")
        return d

    def test_backfills_unarchived_session(self):
        """未归档 + 本机文件还在 → 入队。"""
        self._touch("rec/主播甲/2026-08-28")
        self._write_history([self._entry("主播甲", "rec/主播甲/2026-08-28/主播甲-20260828-044846-%03d.flv")])
        self.assertEqual(nas.backfill_from_history({}, self.hist), 1)
        self.assertEqual(nas.queue_status()["depth"], 1)
        self.assertEqual(nas._pending[0]["streamer"], "主播甲")
        self.assertEqual(nas._pending[0]["date"], "2026-08-28")

    def test_skips_archived(self):
        """已归档的不入队。"""
        self._touch("rec/主播甲/2026-08-28")
        self._write_history([self._entry("主播甲", "rec/主播甲/2026-08-28/x-%03d.flv",
                                         state="archived")])
        self.assertEqual(nas.backfill_from_history({}, self.hist), 0)

    def test_never_enqueues_streamer_dir(self):
        """★ 老格式条目(output_path 无日期层)绝不能把 <主播>/ 整目录入队。

        那是该主播所有场次的父目录,一旦当 src 搬走就是批量误搬。
        """
        self._touch("rec/主播甲/2026-08-28")
        self._touch("rec/主播甲/2026-08-30")
        self._write_history([self._entry("主播甲", "rec/主播甲/主播甲-20260828-044846-%03d.flv")])
        nas.backfill_from_history({}, self.hist)
        srcs = [e["src_path"] for e in nas._pending]
        self.assertNotIn(os.path.join(self.base, "rec", "主播甲"), srcs)
        for s in srcs:
            self.assertTrue(nas._DATE_DIR_RE.match(os.path.basename(s)), s)

    def test_skips_empty_dir(self):
        """目录存在但空 → 不入队(否则搬运后多一个空目录)。"""
        os.makedirs(os.path.join(self.base, "rec/主播甲/2026-08-28"))
        self._write_history([self._entry("主播甲", "rec/主播甲/2026-08-28/x-%03d.flv")])
        self.assertEqual(nas.backfill_from_history({}, self.hist), 0)

    def test_idempotent(self):
        """重复调用不重复入队(目标恢复时会反复触发)。"""
        self._touch("rec/主播甲/2026-08-28")
        self._write_history([self._entry("主播甲", "rec/主播甲/2026-08-28/x-%03d.flv")])
        self.assertEqual(nas.backfill_from_history({}, self.hist), 1)
        self.assertEqual(nas.backfill_from_history({}, self.hist), 0)
        self.assertEqual(nas.queue_status()["depth"], 1)

    def test_missing_history_file(self):
        """history 读不到时返回 0,不抛异常。"""
        self.assertEqual(nas.backfill_from_history({}, os.path.join(self.base, "nope.json")), 0)


class TestPickDirectory(unittest.TestCase):
    """目录选择框:只测降级路径(真正弹窗无法在无 GUI 环境里测)。"""
    def setUp(self):
        self.orig = nas.subprocess.run

    def tearDown(self):
        nas.subprocess.run = self.orig

    def _stub(self, returncode=0, stdout="/Volumes/Disk\n", stderr="", exc=None):
        def run(*a, **kw):
            if exc:
                raise exc
            return subprocess.CompletedProcess(a, returncode, stdout, stderr)
        nas.subprocess.run = run

    def test_returns_path(self):
        self._stub(0, "/Volumes/Disk\n")
        path, err = nas.pick_directory("x", timeout=1)
        self.assertEqual(path, "/Volumes/Disk")
        self.assertIsNone(err)

    def test_cancel_gives_clean_message(self):
        """点「取消」要给出明确文案,不能当成崩溃。"""
        self._stub(1, "", "execution error: User canceled. (-128)")
        path, err = nas.pick_directory("x", timeout=1)
        self.assertIsNone(path)
        self.assertEqual(err, "已取消")

    def test_timeout_degrades(self):
        self._stub(exc=subprocess.TimeoutExpired("osascript", 1))
        path, err = nas.pick_directory("x", timeout=1)
        self.assertIsNone(path)
        self.assertIn("超时", err)

    def test_no_osascript(self):
        self._stub(exc=FileNotFoundError())
        path, err = nas.pick_directory("x", timeout=1)
        self.assertIsNone(path)
        self.assertIn("osascript", err)


class TestSpaceGuardAndResume(unittest.TestCase):
    """盘前空间预检 / 断点续传 / 失败回退 / 进度上报。

    背景:2026-09-18 外接盘 119GB 被写满,搬运在复制到一半时抛 ENOSPC,
    重试 5 次标 failed,而 history 停在 archiving —— 目标里有半套文件、源也没删。
    这一组测试守住修复后的行为。
    """
    def setUp(self):
        nas.DRY_RUN = True
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        nas.SPOOL_DIR = os.path.join(self.base, "spool")
        nas.PENDING_PATH = os.path.join(nas.SPOOL_DIR, "pending.json")
        nas._pending = []
        nas._last_archive = None
        nas.set_archive_hook(None)
        self.mp = os.path.join(self.base, "mount")
        os.makedirs(self.mp)
        self.nas_cfg = {"enabled": True, "host": "h", "share": "s",
                        "mount_point": self.mp, "root_dir": "录播"}
        self.cfg = {"nas": self.nas_cfg}
        self.orig_free = nas._free_bytes
        self.orig_copy2 = nas.shutil.copy2

    def tearDown(self):
        nas._free_bytes = self.orig_free
        nas.shutil.copy2 = self.orig_copy2
        nas.set_archive_hook(None)
        nas.DRY_RUN = False
        self.tmp.cleanup()

    def _src_dir(self, name="s1", files=(("a.flv", 1024),)):
        d = os.path.join(self.base, "src", name, "2026-09-01")
        os.makedirs(d, exist_ok=True)
        for fn, size in files:
            with open(os.path.join(d, fn), "wb") as f:
                f.write(b"x" * size)
        return d

    def _dest_dir(self, name="s1"):
        return os.path.join(self.mp, "录播", name, "2026-09-01")

    # ── ① 空间预检 ──

    def test_wait_space_leaves_everything_untouched(self):
        """空间不足 → 不开口:源不删、目标不建、不消耗重试次数。"""
        d = self._src_dir()
        nas._free_bytes = lambda p: 100          # 只有 100 字节可用
        nas.enqueue({"streamer": "s1", "src_path": d, "date": "2026-09-01"})
        nas.process_queue(self.cfg, target_dir=self.mp)
        e = nas._pending[0]
        self.assertEqual(e["status"], "waiting_space")
        self.assertIn("空间不足", e["error"])
        self.assertEqual(e.get("missing_files"), 1)
        self.assertEqual(e.get("retries", 0), 0)   # 不是失败:重试次数不增加
        self.assertTrue(os.path.isdir(d), "空间不足时不该动源目录")
        self.assertFalse(os.path.exists(self._dest_dir()), "空间不足时不该建目标")

    def test_wait_space_then_resume_when_space_returns(self):
        """腾出空间后,下一轮自动继续(不用重启)。"""
        d = self._src_dir()
        nas._free_bytes = lambda p: 100
        nas.enqueue({"streamer": "s1", "src_path": d, "date": "2026-09-01"})
        nas.process_queue(self.cfg, target_dir=self.mp)
        self.assertEqual(nas._pending[0]["status"], "waiting_space")
        nas._free_bytes = self.orig_free
        nas.process_queue(self.cfg, target_dir=self.mp)
        self.assertEqual(nas._pending, [], "空间恢复后应搬走")
        self.assertTrue(os.path.isfile(os.path.join(self._dest_dir(), "a.flv")))

    def test_enospc_during_copy_is_wait_space_not_retry(self):
        """复制中途 ENOSPC:应判 wait_space(等空间),不是 retry(耗重试)。"""
        d = self._src_dir()
        nas.enqueue({"streamer": "s1", "src_path": d, "date": "2026-09-01"})

        def boom(*a, **k):
            raise OSError(28, "No space left on device")

        nas.shutil.copy2 = boom
        nas.process_queue(self.cfg, target_dir=self.mp)
        e = nas._pending[0]
        self.assertEqual(e["status"], "waiting_space")
        self.assertEqual(e.get("retries", 0), 0)   # 不消耗重试次数
        self.assertIn("空间不足", e["error"])
        self.assertTrue(os.path.isdir(d))

    # ── ② 断点续传 ──

    def test_resume_skips_already_copied_file(self):
        """目标已有同名同大小文件 → 跳过重传(inode 不变即为未重写)。"""
        d = self._src_dir(files=(("a.flv", 1024), ("b.flv", 2048)))
        dest = self._dest_dir()
        os.makedirs(dest, exist_ok=True)
        with open(os.path.join(dest, "a.flv"), "wb") as f:   # 上次中断留下的完整副本
            f.write(b"x" * 1024)
        ino_before = os.stat(os.path.join(dest, "a.flv")).st_ino
        nas.enqueue({"streamer": "s1", "src_path": d, "date": "2026-09-01"})
        nas.process_queue(self.cfg, target_dir=self.mp)
        self.assertEqual(nas._pending, [])
        self.assertEqual(os.stat(os.path.join(dest, "a.flv")).st_ino, ino_before,
                         "已存在的完整文件被重传了")
        self.assertTrue(os.path.isfile(os.path.join(dest, "b.flv")))

    # ── ③ 失败回退 ──

    def test_failure_rolls_history_back_to_pending(self):
        """搬挂了不能把 history 挂在 archiving,要退回 pending 并带上原因。"""
        calls = []
        nas.set_archive_hook(
            lambda src, st, dest=None, target=None, error=None: calls.append((st, error)))
        d = self._src_dir()
        nas.enqueue({"streamer": "s1", "src_path": d, "date": "2026-09-01"})

        def boom(*a, **k):
            raise OSError(28, "No space left on device")

        nas.shutil.copy2 = boom
        nas.process_queue(self.cfg, target_dir=self.mp)
        states = [c[0] for c in calls]
        self.assertIn("archiving", states)
        self.assertIn("pending", states, "失败后 history 没回退,会留下僵尸「归档中」")
        last = [c for c in calls if c[0] == "pending"][-1]
        self.assertIn("No space left", last[1] or "")

    def test_missing_recorded_on_interrupt(self):
        """中断后要记下「还剩哪些没传」。"""
        d = self._src_dir(files=(("a.flv", 1024), ("b.flv", 2048)))
        nas.enqueue({"streamer": "s1", "src_path": d, "date": "2026-09-01"})

        def boom(*a, **k):
            raise OSError(28, "No space left on device")

        nas.shutil.copy2 = boom
        nas.process_queue(self.cfg, target_dir=self.mp)
        e = nas._pending[0]
        self.assertEqual(e.get("missing_files"), 2)
        self.assertEqual(e.get("missing_bytes"), 1024 + 2048)

    # ── ④ 进度 ──

    def test_progress_reported_during_transfer_and_cleared_after(self):
        ticks = []
        orig = nas._progress_tick

        def spy(name, size):
            ticks.append(name)
            self.assertIsNotNone(nas.progress_status(), "搬运中应能看到进度")
            orig(name, size)

        nas._progress_tick = spy
        try:
            d = self._src_dir(files=(("a.flv", 1024), ("b.flv", 2048)))
            nas.enqueue({"streamer": "s1", "src_path": d, "date": "2026-09-01"})
            nas.process_queue(self.cfg, target_dir=self.mp)
        finally:
            nas._progress_tick = orig
        self.assertEqual(sorted(ticks), ["a.flv", "b.flv"])
        self.assertIsNone(nas.progress_status(), "搬完应清空进度")

    def test_scan_missing_detects_absent_and_size_mismatch(self):
        """_scan_missing:目标缺文件 / 大小不符 都算未传。"""
        src = os.path.join(self.base, "s")
        dest = os.path.join(self.base, "d")
        os.makedirs(os.path.join(src, ".meta"))
        os.makedirs(dest)
        for p, n in ((os.path.join(src, "a.flv"), 10),
                     (os.path.join(src, "b.flv"), 20),
                     (os.path.join(src, ".meta", "c.jsonl"), 5)):
            with open(p, "wb") as f:
                f.write(b"x" * n)
        with open(os.path.join(dest, "a.flv"), "wb") as f:      # 已传且大小一致
            f.write(b"x" * 10)
        with open(os.path.join(dest, "b.flv"), "wb") as f:      # 大小不符(传了一半)
            f.write(b"x" * 3)
        missing, need = nas._scan_missing(src, dest)
        self.assertEqual(sorted(missing), [".meta/c.jsonl", "b.flv"])
        self.assertEqual(need, 25)


class TestProbeWritable(unittest.TestCase):
    """可写性探针只以「写入成功」为准。

    删不掉探针不能算不可写:上层 ensure_mount 一旦拿到 False,就会走僵尸挂载
    分支 umount -f 把卷卸掉。让一个 5 字节探针的删除失败引发卸载正在归档的
    NAS,代价完全不对等。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_writable_dir_returns_true(self):
        self.assertTrue(nas._probe_writable(self.d))

    def test_full_disk_still_counts_writable(self):
        """盘满(ENOSPC)要判「可写」——否则会误报成「目标离线」,真因(没空间)被藏起来。"""
        orig = open

        def boom(path, *a, **k):
            if str(path).endswith(".douyin-probe"):
                raise OSError(28, "No space left on device")
            return orig(path, *a, **k)

        import builtins
        builtins.open = boom
        try:
            self.assertTrue(nas._probe_writable(self.d))
        finally:
            builtins.open = orig

    def test_missing_dir_returns_false(self):
        self.assertFalse(nas._probe_writable(os.path.join(self.d, "nope")))

    def test_remove_failure_still_writable(self):
        """删除探针失败(占用/权限/安全软件)仍应判定为可写。"""
        orig = os.remove
        os.remove = lambda p: (_ for _ in ()).throw(OSError("busy"))
        try:
            self.assertTrue(nas._probe_writable(self.d))
        finally:
            os.remove = orig
        # 探针确实留下了,下次会被覆盖写
        self.assertTrue(os.path.exists(os.path.join(self.d, ".douyin-probe")))


class TestTestConnection(unittest.TestCase):
    """「测试连接」的两条分支都不能因为探针删不掉而误报失败。

    NAS 上 os.remove 可能因回收站/占用/安全软件失败(见 TestProbeWritable),
    而用户看到的就是「连接失败」—— 实际卷是好的,纯属探针清理的副作用。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.orig_dry = nas.DRY_RUN
        nas.DRY_RUN = False
        self.cfg = {"host": "192.168.1.2", "share": "share_video",
                    "username": "u", "port": 445, "mount_point": self.d}

    def tearDown(self):
        nas.DRY_RUN = self.orig_dry
        self.tmp.cleanup()

    def _patch(self, **kw):
        orig = {k: getattr(nas, k) for k in kw}
        for k, v in kw.items():
            setattr(nas, k, v)
        return orig

    def _restore(self, orig):
        for k, v in orig.items():
            setattr(nas, k, v)

    def test_mounted_branch_ignores_probe_remove_failure(self):
        """已挂载 + 可写,即使探针删不掉也要报成功。"""
        orig = self._patch(_share_mounted=lambda h, s: True)
        real_remove = os.remove
        os.remove = lambda p: (_ for _ in ()).throw(OSError("FSMoveObjectToTrashSync"))
        try:
            r = nas.test_connection(self.cfg)
        finally:
            os.remove = real_remove
            self._restore(orig)
        self.assertTrue(r["ok"], r.get("error"))
        self.assertIsNone(r["error"])

    def test_mounted_branch_unwritable_reports_failure(self):
        """已挂载但确实写不进去时,才报「已挂载但写入失败」。"""
        orig = self._patch(_share_mounted=lambda h, s: True,
                           _probe_writable=lambda p: False)
        try:
            r = nas.test_connection(self.cfg)
        finally:
            self._restore(orig)
        self.assertFalse(r["ok"])
        self.assertIn("已挂载但写入失败", r["error"])

    def test_temp_mount_branch_ignores_probe_remove_failure(self):
        """临时挂载分支:卸载前的探针不做删除,删除失败不应影响结果。"""
        calls = {}

        def fake_mount(host, share, port, username, password, mp):
            calls["mounted"] = mp
            return True, None

        def fake_umount(*a, **k):
            class R:
                returncode = 0
                stdout = ""
            return R()

        orig = self._patch(_share_mounted=lambda h, s: False,
                           _do_mount=fake_mount)
        real_remove = os.remove
        real_run = subprocess.run
        os.remove = lambda p: (_ for _ in ()).throw(OSError("busy"))
        subprocess.run = fake_umount
        try:
            r = nas.test_connection(self.cfg)
        finally:
            os.remove = real_remove
            subprocess.run = real_run
            self._restore(orig)
        self.assertTrue(r["ok"], r.get("error"))

    def test_missing_host_share_rejected(self):
        for cfg in ({"host": "", "share": "s"}, {"host": "h", "share": ""}):
            r = nas.test_connection(cfg)
            self.assertFalse(r["ok"])


if __name__ == "__main__":
    unittest.main()


class TestDeferWhileRecording(unittest.TestCase):
    """正在录制的场次绝不能搬 —— 2026-09-18 的真实数据丢失事故。

    事故经过:归档单元是「日期目录」,而某个日期目录里正躺着 ffmpeg 在写的 .flv。
    整目录搬完后 `shutil.rmtree(src)` 把源目录删了,ffmpeg 的写句柄随即指向一个
    已删除的 inode —— 后续数据全写进「幽灵文件」,进程退出时永久丢失
    (实测该文件已涨到 49.5MB),而目标盘上只留一个被截断的 21MB .flv。
    正确行为:整条目 defer —— 不消耗重试、不改 history、绝不 rmtree。
    """
    def setUp(self):
        nas.DRY_RUN = True
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        nas.SPOOL_DIR = os.path.join(self.base, "spool")
        nas.PENDING_PATH = os.path.join(nas.SPOOL_DIR, "pending.json")
        nas._pending = []
        nas._last_archive = None
        nas.set_archive_hook(None)
        nas.set_active_probe(None)
        self.mp = os.path.join(self.base, "mount")
        os.makedirs(self.mp)
        self.nas_cfg = {"enabled": True, "host": "h", "share": "s",
                        "mount_point": self.mp, "root_dir": "录播"}
        self.cfg = {"nas": self.nas_cfg}

    def tearDown(self):
        nas.set_active_probe(None)
        nas.set_archive_hook(None)
        nas.DRY_RUN = False
        self.tmp.cleanup()

    def _src_dir(self, name="s1", files=(("a.flv", 1024), ("b.ass", 32)),
                 date="2026-09-01"):
        d = os.path.join(self.base, "src", name, date)
        os.makedirs(d, exist_ok=True)
        for fn, size in files:
            with open(os.path.join(d, fn), "wb") as f:
                f.write(b"x" * size)
        return d

    def _dest(self, name="s1", date="2026-09-01"):
        return os.path.join(self.mp, "录播", name, date)

    # ── 探针基础语义 ──
    def test_is_recording_path(self):
        d = "/tmp/x/主播/2026-09-01"
        nas.set_active_probe(lambda: [d])
        self.assertTrue(nas.is_recording_path(d))
        self.assertTrue(nas.is_recording_path(d + "/"))          # 规范化
        self.assertTrue(nas.is_recording_path("/tmp/x/主播"))     # 父路径(其内有在录的)
        self.assertFalse(nas.is_recording_path("/tmp/x/其他主播/2026-09-01"))
        self.assertFalse(nas.is_recording_path("/tmp/x/主播2026-09-01"))  # 前缀不算
        self.assertFalse(nas.is_recording_path(""))

    def test_missing_probe_means_nothing_deferred(self):
        """探针未注入(纯脚本调用)时不能把一切当在录 —— 退回老行为。"""
        nas.set_active_probe(None)
        self.assertEqual(nas.active_dirs(), set())
        self.assertFalse(nas.is_recording_path("/any/path"))

    def test_broken_probe_does_not_break_archiving(self):
        """探针抛异常不能连带炸掉归档(宁可漏判也不能整条链路挂掉)。"""
        def boom():
            raise RuntimeError("probe broken")
        nas.set_active_probe(boom)
        self.assertEqual(nas.active_dirs(), set())
        self.assertFalse(nas.is_recording_path("/any/path"))
        d = self._src_dir()
        nas.enqueue({"streamer": "s1", "src_path": d, "date": "2026-09-01"})
        nas.process_queue(self.cfg)
        self.assertEqual(nas.queue_status()["depth"], 0)      # 照常搬完
        self.assertFalse(os.path.exists(d))

    # ── 主守卫:在录就整条目暂缓 ──
    def test_recording_session_is_deferred_not_archived(self):
        d = self._src_dir()
        nas.set_active_probe(lambda: [d])
        nas.enqueue({"streamer": "s1", "src_path": d, "date": "2026-09-01"})
        nas.process_queue(self.cfg)

        self.assertEqual(nas.queue_status()["deferred"], 1)
        self.assertEqual(nas.queue_status()["failed"], 0)
        e = nas._pending[0]
        self.assertEqual(e["status"], "deferred")
        self.assertEqual(e.get("retries", 0), 0)            # 不消耗重试
        self.assertIn("正在录制", e["error"])
        self.assertTrue(os.path.isdir(d))                   # ★ 源目录必须还在
        self.assertTrue(os.path.exists(os.path.join(d, "a.flv")))
        self.assertFalse(os.path.isdir(self._dest()))       # 一个字节都没往目标写

    def test_defer_logged_only_once(self):
        """每 60s 轮一次,不能每轮都刷日志。"""
        d = self._src_dir()
        nas.set_active_probe(lambda: [d])
        nas.enqueue({"streamer": "s1", "src_path": d, "date": "2026-09-01"})
        lines = []
        orig = nas._log
        nas._log = lines.append
        try:
            for _ in range(4):
                nas.process_queue(self.cfg)
        finally:
            nas._log = orig
        self.assertEqual(len([x for x in lines if "归档暂缓" in x]), 1, lines)

    def test_recording_stop_then_archive_succeeds(self):
        """录完(探针里消失)⇒ 下一轮自动搬走,无需人工干预。"""
        d = self._src_dir()
        active = {d}
        nas.set_active_probe(lambda: list(active))
        nas.enqueue({"streamer": "s1", "src_path": d, "date": "2026-09-01"})
        nas.process_queue(self.cfg)
        self.assertEqual(nas.queue_status()["deferred"], 1)

        active.clear()                                       # 录制结束
        nas.process_queue(self.cfg)
        self.assertEqual(nas.queue_status()["depth"], 0)
        self.assertTrue(os.path.exists(os.path.join(self._dest(), "a.flv")))
        self.assertFalse(os.path.exists(d))

    # ── 第二道闸:搬运期间开始录制,rmtree 前必须中止 ──
    def test_started_recording_mid_copy_aborts_before_rmtree(self):
        """入口时没在录、复制过程中开录 ⇒ 必须在 rmtree 前刹住(源目录保留)。

        这是事故最可能的成因:归档刚判定「可搬」,同一日期目录里又起了新一场录制。
        """
        d = self._src_dir()
        calls = {"n": 0}
        real_copy2 = nas.shutil.copy2

        def probe():
            calls["n"] += 1
            return [] if calls["n"] <= 1 else [d]            # 第 2 次起:已在录制

        nas.set_active_probe(probe)

        def copy2(s, p, **k):
            real_copy2(s, p, **k)                            # 正常复制
            return None

        nas.shutil.copy2 = copy2
        try:
            nas.enqueue({"streamer": "s1", "src_path": d, "date": "2026-09-01"})
            nas.process_queue(self.cfg)
        finally:
            nas.shutil.copy2 = real_copy2

        self.assertTrue(os.path.isdir(d), "源目录被删了 —— 正在录制的文件会写进幽灵文件")
        self.assertTrue(os.path.exists(os.path.join(d, "a.flv")))
        self.assertEqual(nas._pending[0]["status"], "deferred")
        self.assertEqual(nas._pending[0].get("retries", 0), 0)
        # 已复制的文件留在目标(下轮按同名同大小跳过),不算错
        self.assertTrue(os.path.exists(os.path.join(self._dest(), "a.flv")))

    # ── 第三道闸:文件在复制时长大了 ⇒ 也算在写 ──
    def test_growing_file_defers_instead_of_retry(self):
        """源文件复制期间变大(探针没覆盖到)⇒ defer,而不是失败重试。"""
        d = self._src_dir(files=(("live.flv", 512),))
        nas.set_active_probe(None)
        real_copy2 = nas.shutil.copy2

        def growing(s, p, **k):
            # 在「取大小快照」之后、真正复制之前让源变大 —— 等价于复制期间被写入
            with open(s, "ab") as f:
                f.write(b"y" * 256)
            real_copy2(s, p, **k)

        nas.shutil.copy2 = growing
        try:
            nas.enqueue({"streamer": "s1", "src_path": d, "date": "2026-09-01"})
            nas.process_queue(self.cfg)
        finally:
            nas.shutil.copy2 = real_copy2

        self.assertEqual(nas._pending[0]["status"], "deferred")
        self.assertEqual(nas._pending[0].get("retries", 0), 0)
        self.assertTrue(os.path.isdir(d))
        self.assertFalse(os.path.exists(os.path.join(self._dest(), "a.flv")))

    def test_old_stale_size_mismatch_still_retries(self):
        """文件很旧(超过写入宽限期)的大小不一致仍是真错误 → 继续走重试。"""
        d = self._src_dir(files=(("old.flv", 512),))
        os.utime(os.path.join(d, "old.flv"), (time.time() - 9999,) * 2)
        nas.set_active_probe(None)
        real_copy2 = nas.shutil.copy2

        def corrupt(s, p, **k):
            real_copy2(s, p, **k)
            with open(p, "ab") as f:                         # 目标写多了:真错误
                f.write(b"z" * 100)

        nas.shutil.copy2 = corrupt
        try:
            nas.enqueue({"streamer": "s1", "src_path": d, "date": "2026-09-01"})
            nas.process_queue(self.cfg)
        finally:
            nas.shutil.copy2 = real_copy2

        e = nas._pending[0]
        self.assertEqual(e["status"], "pending")
        self.assertEqual(e["retries"], 1)
        self.assertIn("复制后大小不一致", e["error"])

    def test_no_part_left_over_when_deferred(self):
        """.part 半成品必须清掉,否则下一轮会把它当已传文件。"""
        d = self._src_dir(files=(("live.flv", 512),))
        real_copy2 = nas.shutil.copy2

        def growing(s, p, **k):
            with open(s, "ab") as f:
                f.write(b"y" * 256)
            real_copy2(s, p, **k)

        nas.shutil.copy2 = growing
        try:
            nas.enqueue({"streamer": "s1", "src_path": d, "date": "2026-09-01"})
            nas.process_queue(self.cfg)
        finally:
            nas.shutil.copy2 = real_copy2
        parts = []
        for root, _dirs, files in os.walk(self.mp):
            parts += [f for f in files if f.endswith(".part")]
        self.assertEqual(parts, [])

    # ── 状态字段 ──
    def test_status_exposes_deferred_count(self):
        d = self._src_dir()
        nas.set_active_probe(lambda: [d])
        nas.enqueue({"streamer": "s1", "src_path": d, "date": "2026-09-01"})
        nas.process_queue(self.cfg)
        self.assertEqual(nas.queue_status()["deferred"], 1)
        st = nas._nas_status(True, 0, "external", self.mp)
        self.assertEqual(st["deferred"], 1)

    # ── 补归档也要跳过在录场次 ──
    def test_backfill_skips_recording_session(self):
        d = self._src_dir()
        hist = [{"name": "s1", "status": "done",
                 "output_path": os.path.join(d, "s1-20260901-010101-%03d.flv"),
                 "started_at": 1, "ended_at": 2, "archive": {"state": "none"}}]
        p = os.path.join(self.base, "history.json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump(hist, f)

        nas.set_active_probe(lambda: [d])
        self.assertEqual(nas.backfill_from_history(self.cfg, history_path=p), 0)
        self.assertEqual(nas._pending, [])

        nas.set_active_probe(None)                           # 录完了
        self.assertEqual(nas.backfill_from_history(self.cfg, history_path=p), 1)


class TestArchivedStateIsTerminal(unittest.TestCase):
    """已归档是终态,不得被陈旧队列条目的失败回调降级。

    2026-09-18 实际发生:11 条 history 从 archived 被覆写成
    「failed / 源不存在」—— 文件明明已在新盘上,历史页却显示归档失败。
    """
    def test_archived_state_not_downgraded_by_stale_queue_entry(self):
        import monitor
        orig_hist = monitor._history
        committed = {"n": 0}
        orig_commit = monitor._commit_history
        monitor._commit_history = lambda: committed.__setitem__("n", committed["n"] + 1)
        try:
            monitor._history = [{
                "name": "s1",
                "output_path": "/rec/s1/2026-09-01/s1-20260901-010101-%03d.flv",
                "status": "done",
                "archive": {"state": "archived", "dest": "/mnt/s1/2026-09-01",
                            "at": 111.0, "error": None},
            }]
            d = "/rec/s1/2026-09-01"
            monitor._mark_archive_state(d, "failed", None, None, "源不存在")
            a = monitor._history[0]["archive"]
            self.assertEqual(a["state"], "archived", "已归档被降级成失败了")
            self.assertIsNone(a["error"])
            self.assertEqual(a["at"], 111.0)
            self.assertEqual(committed["n"], 0, "没变化就不该写盘")

            # 反向:非终态之间仍可正常流转
            monitor._mark_archive_state(d, "pending", None, None, "x")
            self.assertEqual(monitor._history[0]["archive"]["state"], "archived")
        finally:
            monitor._history = orig_hist
            monitor._commit_history = orig_commit


class TestArchiveTargetPassedThrough(unittest.TestCase):
    """归档回写必须带上目标类型(nas/external)——历史页才能显示「归档到哪」。

    2026-09-21 之前 _notify_archive 的 target 参数从未被填,前端把 archived
    一律显示成「归档到 NAS」,用户挂的是外接硬盘也被写 NAS。
    """

    def setUp(self):
        nas.DRY_RUN = True
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self._old = (nas.SPOOL_DIR, nas.PENDING_PATH, nas._pending,
                     nas._last_archive, nas._archive_hook, nas._active_probe)
        nas.SPOOL_DIR = os.path.join(self.base, "spool")
        nas.PENDING_PATH = os.path.join(nas.SPOOL_DIR, "pending.json")
        nas._pending = []
        nas._last_archive = None
        self.mp = os.path.join(self.base, "mount")
        os.makedirs(self.mp)
        self.nas_cfg = {"enabled": True, "host": "h", "share": "s",
                        "mount_point": self.mp, "root_dir": "录播"}
        self.cfg = {"nas": self.nas_cfg}
        self.seen = []
        nas.set_archive_hook(
            lambda src, st, dest, target, err: self.seen.append((st, dest, target, err)))

    def tearDown(self):
        nas.set_archive_hook(None)
        nas.set_active_probe(None)
        (nas.SPOOL_DIR, nas.PENDING_PATH, nas._pending,
         nas._last_archive, nas._archive_hook, nas._active_probe) = self._old
        self.tmp.cleanup()

    def _src(self, files=(("a.flv", 512), ("a.ass", 32))):
        d = os.path.join(self.base, "src", "s1", "2026-09-21")
        os.makedirs(d, exist_ok=True)
        for fn, size in files:
            with open(os.path.join(d, fn), "wb") as f:
                f.write(b"x" * size)
        return d

    def test_external_mode_reports_external(self):
        d = self._src()
        nas.enqueue({"streamer": "s1", "src_path": d, "date": "2026-09-21"})
        nas.process_queue(self.cfg, target_dir=self.mp, mode="external")
        states = [s for s, *_ in self.seen]
        self.assertIn("archived", states)
        # 所有回写的 target 都是 external
        for st, dest, target, err in self.seen:
            self.assertEqual(target, "external", f"{st} 回写 target={target}")

    def test_nas_mode_reports_nas(self):
        d = self._src()
        nas.enqueue({"streamer": "s1", "src_path": d, "date": "2026-09-21"})
        nas.process_queue(self.cfg, target_dir=self.mp, mode="nas")
        for st, dest, target, err in self.seen:
            self.assertEqual(target, "nas", f"{st} 回写 target={target}")

    def test_default_mode_none_still_archives(self):
        """不传 mode(旧调用方)不炸,target 为 None 但归档照常完成。"""
        d = self._src()
        nas.enqueue({"streamer": "s1", "src_path": d, "date": "2026-09-21"})
        nas.process_queue(self.cfg, target_dir=self.mp)
        self.assertEqual([s for s, *_ in self.seen].count("archived"), 1)
