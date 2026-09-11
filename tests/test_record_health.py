#!/usr/bin/env python3
"""录制健康度(防「虚假在线」)与弹幕面板路径解析 的单元测试(临时目录,不碰真实数据)。"""
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


class FakeProc:
    """假 ffmpeg:alive=False 时 poll() 返回退出码(模拟进程已退出)。"""

    def __init__(self):
        self.alive = True
        self.terminated = False

    def poll(self):
        return None if self.alive else 1

    def terminate(self):
        self.alive = False
        self.terminated = True

    def kill(self):
        self.alive = False

    def wait(self, timeout=None):
        return 0


class FakeState:
    def __init__(self, cfg):
        self.cfg = cfg
        self.recordings = {}
        self.history = []

    def get_config(self):
        return self.cfg

    def set_recording(self, name, info):
        self.recordings[name] = info

    def remove_recording(self, name):
        self.recordings.pop(name, None)

    def set_history(self, h):
        self.history = h


class TestRecordingHealth(unittest.TestCase):
    """虚假在线三条止损:数据停滞重启 / 重启上限 / 换房间重启。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.rec = os.path.join(self.base, "recordings")
        os.makedirs(self.rec, exist_ok=True)
        self.cfg = {
            "recorder": {"output_dir": self.rec, "enabled": True,
                         "stall_seconds": 90, "stall_max_restarts": 3},
            "danmaku": {"enabled": False},
            "nas": {"enabled": False},
            "archive": {"mode": "auto", "external_dir": ""},
        }
        self.state = FakeState(self.cfg)
        self.started = []

        def fake_start(state, name, url, cfg, room_id=None):
            self.started.append((name, url, room_id))
            out = os.path.join(self.rec, name, DAY, f"{name}-%03d.flv")
            os.makedirs(os.path.dirname(out), exist_ok=True)
            monitor._recordings[name] = {
                "proc": FakeProc(), "started_at": time.time(), "output_path": out,
                "stream_url": url, "format": "flv", "room_id": room_id}
            return monitor._recordings[name]["proc"]

        self.orig_start = monitor._start_recording
        self.orig_finish = monitor._history_finish
        monitor._start_recording = fake_start
        monitor._history_finish = lambda state, name: None
        monitor._recordings = {}
        monitor._rec_health = {}

    def tearDown(self):
        monitor._start_recording = self.orig_start
        monitor._history_finish = self.orig_finish
        monitor._recordings = {}
        monitor._rec_health = {}
        monitor._rec_giveup_until = {}
        self.tmp.cleanup()

    def _seed(self, name="主播A", room_id="100", size=1000):
        """造一个「正在录制」的场次:目录下已有 size 字节的分片。"""
        out = os.path.join(self.rec, name, DAY, f"{name}-%03d.flv")
        d = os.path.dirname(out)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, f"{name}-000.flv"), "wb") as f:
            f.write(b"x" * size)
        monitor._recordings[name] = {
            "proc": FakeProc(), "started_at": time.time(), "output_path": out,
            "stream_url": "http://live/old", "format": "flv", "room_id": room_id}
        monitor._rec_health[name] = {"size": size, "ts": time.time(),
                                     "restarts": 0, "room_id": room_id,
                                     "stalled": False}
        return out

    # ── 1. 已落盘字节数统计 ──
    def test_output_size_sums_segments(self):
        out = self._seed(size=100)
        d = os.path.dirname(out)
        with open(os.path.join(d, f"{'主播A'}-001.flv"), "wb") as f:
            f.write(b"y" * 250)
        self.assertEqual(monitor._rec_output_size(out), 350)

    def test_output_size_ignores_sidecar(self):
        """.ass / .jsonl 等随行文件不计入(它们由弹幕模块独立写入)。"""
        out = self._seed(size=100)
        d = os.path.dirname(out)
        with open(os.path.join(d, "主播A-20260829-120000-000.ass"), "w") as f:
            f.write("x" * 5000)
        self.assertEqual(monitor._rec_output_size(out), 100)

    def test_output_size_missing_dir(self):
        self.assertIsNone(monitor._rec_output_size("/no/such/dir/x-%03d.flv"))

    # ── 2. 文件增长正常:不重启,且 stalled=False ──
    def test_normal_growth_no_restart(self):
        out = self._seed(size=1000)
        d = os.path.dirname(out)
        with open(os.path.join(d, "主播A-000.flv"), "ab") as f:
            f.write(b"z" * 500)
        monitor._refresh_recording(self.state, "主播A", "http://live/new",
                                   self.cfg, "100")
        self.assertEqual(len(self.started), 0)          # 没重启
        self.assertEqual(monitor._rec_health["主播A"]["size"], 1500)
        self.assertFalse(self.state.recordings["主播A"]["stalled"])

    # ── 3. 数据停滞:进程存活但无写入 → 重启 ──
    def test_stall_triggers_restart(self):
        self._seed(size=1000)
        monitor._rec_health["主播A"]["ts"] = time.time() - 120  # 停滞 120s > 90s
        monitor._refresh_recording(self.state, "主播A", "http://live/new",
                                   self.cfg, "100")
        self.assertEqual(len(self.started), 1)
        self.assertEqual(monitor._rec_health["主播A"]["restarts"], 1)

    def test_stall_warns_before_threshold(self):
        """停滞过半阈值(45s)即前端告警,但不重启(避免误判正常缓冲抖动)。"""
        self._seed(size=1000)
        monitor._rec_health["主播A"]["ts"] = time.time() - 60  # 45 < 60 < 90
        monitor._refresh_recording(self.state, "主播A", "http://live/new",
                                   self.cfg, "100")
        self.assertEqual(len(self.started), 0)                      # 未重启
        self.assertTrue(self.state.recordings["主播A"]["stalled"])    # 已告警
        self.assertGreaterEqual(self.state.recordings["主播A"]["stall_seconds"], 60)

    def test_short_stall_no_warning(self):
        """不足半阈值(45s)视为正常抖动,不告警也不重启。"""
        self._seed(size=1000)
        monitor._rec_health["主播A"]["ts"] = time.time() - 20
        monitor._refresh_recording(self.state, "主播A", "http://live/new",
                                   self.cfg, "100")
        self.assertEqual(len(self.started), 0)
        self.assertFalse(self.state.recordings["主播A"]["stalled"])

    # ── 4. 重启上限:超过后停止本场,不再空转刷历史 ──
    def test_stall_max_restarts_stops_recording(self):
        self._seed(size=1000)
        monitor._rec_health["主播A"].update({"restarts": 3,
                                             "ts": time.time() - 120})
        monitor._refresh_recording(self.state, "主播A", "http://live/new",
                                   self.cfg, "100")
        self.assertEqual(len(self.started), 0)                 # 未再重启
        self.assertNotIn("主播A", monitor._recordings)          # 已停止
        self.assertNotIn("主播A", monitor._rec_health)

    # ── 5. 换房间:room_id 变化强制开新文件 ──
    def test_room_id_change_forces_restart(self):
        self._seed(size=1000, room_id="100")
        monitor._refresh_recording(self.state, "主播A", "http://live/new",
                                   self.cfg, "200")
        self.assertEqual(len(self.started), 1)
        self.assertEqual(self.started[0][2], "200")            # 新场次带上新 room_id
        self.assertEqual(monitor._recordings["主播A"]["room_id"], "200")

    def test_same_room_id_no_restart(self):
        self._seed(size=1000, room_id="100")
        monitor._refresh_recording(self.state, "主播A", "http://live/new",
                                   self.cfg, "100")
        self.assertEqual(len(self.started), 0)

    # ── 6. 进程退出:仍照旧重启 ──
    def test_dead_process_restarts(self):
        self._seed(size=1000)
        monitor._recordings["主播A"]["proc"].alive = False
        monitor._refresh_recording(self.state, "主播A", "http://live/new",
                                   self.cfg, "100")
        self.assertEqual(len(self.started), 1)

    # ── 6b. 进程反复退出:同样受重启上限约束 + 冷却(防止巡检每 20s 刷一个空文件)──
    def test_repeated_exit_hits_limit_then_cools_down(self):
        """流地址失效时 ffmpeg 一启动就退出;不设闸会每轮巡检重开一个空文件。"""
        self._seed(size=0)
        monitor._recordings["主播A"]["proc"].alive = False
        for i in range(6):
            monitor._refresh_recording(self.state, "主播A", "http://live/new",
                                       self.cfg, "100")
            if monitor._recordings.get("主播A"):
                monitor._recordings["主播A"]["proc"].alive = False
        self.assertLessEqual(len(self.started), 3)          # 上限 3 次即止
        self.assertNotIn("主播A", monitor._recordings)       # 已停止
        self.assertIn("主播A", monitor._rec_giveup_until)    # 进入冷却

    def test_cooldown_blocks_restart(self):
        """冷却期内不再为该主播开新录制(即便检测到直播)。"""
        monitor._rec_giveup_until["主播A"] = time.time() + 300
        monitor._refresh_recording(self.state, "主播A", "http://live/new",
                                   self.cfg, "100")
        self.assertEqual(len(self.started), 0)
        self.assertNotIn("主播A", monitor._recordings)

    def test_new_room_id_clears_cooldown(self):
        """换房间 = 新的一场,应解除上一场的冷却。"""
        self._seed(size=1000, room_id="100")
        monitor._rec_giveup_until["主播A"] = time.time() + 300
        monitor._refresh_recording(self.state, "主播A", "http://live/new",
                                   self.cfg, "200")
        self.assertEqual(len(self.started), 1)
        self.assertNotIn("主播A", monitor._rec_giveup_until)

    # ── 7. 无流地址:已有录制不打断 ──
    def test_no_stream_url_keeps_recording(self):
        self._seed(size=1000)
        monitor._refresh_recording(self.state, "主播A", "", self.cfg, "100")
        self.assertEqual(len(self.started), 0)
        self.assertIn("主播A", monitor._recordings)


class TestDanmakuPathResolve(unittest.TestCase):
    """弹幕面板路径解析:归档后应指向归档目标,而非已失效的本机路径。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.rec = os.path.join(self.base, "recordings")
        self.nas = os.path.join(self.base, "nas")
        self.ext = os.path.join(self.base, "ext")
        for d in (self.rec, self.nas, self.ext):
            os.makedirs(d, exist_ok=True)
        self.cfg = {
            "recorder": {"output_dir": self.rec},
            "nas": {"enabled": True, "mount_point": self.nas, "root_dir": "直播回放"},
            "archive": {"mode": "auto", "external_dir": self.ext},
        }
        self.state = FakeState(self.cfg)
        self.orig_hist = monitor._history
        monitor._history = []

    def tearDown(self):
        monitor._history = self.orig_hist
        self.tmp.cleanup()

    def _session(self, name="主播A", state="none", dest=None):
        src_dir = os.path.join(self.rec, name, DAY)
        os.makedirs(src_dir, exist_ok=True)
        out = os.path.join(src_dir, f"{name}-%03d.flv")
        e = {"name": name, "started_at": time.time(), "output_path": out,
             "archive": {"state": state, "dest": dest, "target": None,
                         "at": None, "error": None}}
        monitor._history = [e]
        return out

    # ── 1. 归档后重定位到 NAS(修复「已归档场次误报无弹幕」的核心)──
    def test_archived_resolves_to_dest(self):
        out = self._session()
        dest = os.path.join(self.nas, "直播回放", "主播A", DAY)
        os.makedirs(dest, exist_ok=True)
        monitor._history[0]["archive"] = {"state": "archived", "dest": dest,
                                          "target": "nas", "at": time.time(),
                                          "error": None}
        got = webui._danmaku_safe_output(self.state, out)
        self.assertTrue(got and got.startswith(dest))
        self.assertIn("-%03d.flv", got)

    def test_archived_external_dir(self):
        out = self._session()
        dest = os.path.join(self.ext, "主播A", DAY)
        os.makedirs(dest, exist_ok=True)
        monitor._history[0]["archive"] = {"state": "archived", "dest": dest,
                                          "target": "external", "at": time.time(),
                                          "error": None}
        got = webui._danmaku_safe_output(self.state, out)
        self.assertTrue(got and got.startswith(dest))

    # ── 2. 本机文件还在 → 沿用本机路径 ──
    def test_local_alive_uses_local(self):
        out = self._session()
        d = os.path.dirname(out)
        for fn in ("主播A-000.flv", "主播A-001.flv"):
            open(os.path.join(d, fn), "wb").write(b"x")
        got = webui._danmaku_safe_output(self.state, out)
        self.assertEqual(got, out)

    # ── 3. 未归档且本机已删 → 不重定位(没有归档目标可指)──
    def test_not_archived_stays_local(self):
        out = self._session(state="none")
        got = webui._danmaku_safe_output(self.state, out)
        self.assertEqual(got, out)

    # ── 4. 安全:路径穿越 / 白名单外路径一律拒绝 ──
    def test_rejects_outside_whitelist(self):
        self.assertIsNone(webui._danmaku_safe_output(self.state, "/etc/passwd"))
        self.assertIsNone(webui._danmaku_safe_output(
            self.state, os.path.join(self.base, "secret", "x-%03d.flv")))
        self.assertIsNone(webui._danmaku_safe_output(self.state, ""))
        self.assertIsNone(webui._danmaku_safe_output(self.state, None))

    def test_rejects_traversal_via_archive(self):
        """归档 dest 被篡改成白名单外时,重定位结果也必须被拒。"""
        out = self._session()
        monitor._history[0]["archive"] = {"state": "archived",
                                          "dest": "/etc", "target": "nas"}
        self.assertIsNone(webui._danmaku_safe_output(self.state, out))

    # ── 5. 统一前置校验:NAS 未挂载时给出明确错误 ──
    def test_guard_reports_unreachable(self):
        out = self._session()
        dest = os.path.join(self.nas, "直播回放", "主播A", DAY)
        monitor._history[0]["archive"] = {"state": "archived", "dest": dest,
                                          "target": "nas"}
        # nas.enabled=True 但挂载点里没有内容 → _nas_mounted 判 False
        res = webui._danmaku_guard(self.state, out)
        self.assertEqual(res[0], None)
        self.assertIn("不可访问", res[1]["error"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
