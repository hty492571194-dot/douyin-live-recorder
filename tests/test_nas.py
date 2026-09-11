#!/usr/bin/env python3
"""nas.py 单元测试(DRY_RUN + 临时目录,不依赖真实 NAS)。"""
import json
import os
import subprocess
import tempfile
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

    def test_failure_retries_then_failed(self):
        """源不存在 → 每次搬运 retries+1,≥5 标 failed。"""
        nas.enqueue({"streamer": "s2", "src_path": "/nonexistent/x.mp4"})
        for _ in range(5):
            nas.process_queue(self._cfg())
        qs = nas.queue_status()
        self.assertEqual(qs["failed"], 1)
        self.assertEqual(qs["depth"], 0)
        entry = nas._pending[0]
        self.assertEqual(entry["status"], "failed")
        self.assertGreaterEqual(entry["retries"], 5)

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
