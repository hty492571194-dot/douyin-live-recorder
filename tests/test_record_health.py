#!/usr/bin/env python3
"""录制健康度(防「虚假在线」)与弹幕面板路径解析 的单元测试(临时目录,不碰真实数据)。"""
import asyncio
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
        self.streamers = {}      # 录制看护循环用:主播快照(检测口径)
        self.refreshed = []      # 记录被请求提前复检的主播

    def get_config(self):
        return self.cfg

    def set_recording(self, name, info):
        self.recordings[name] = info

    def remove_recording(self, name):
        self.recordings.pop(name, None)

    def set_history(self, h):
        self.history = h

    def set_streamer(self, name, snap):
        self.streamers[name] = snap

    def get_streamers(self):
        return self.streamers

    def request_refresh(self, name):
        self.refreshed.append(name)


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

class FakeStore:
    """_snap 只需要 store.windows(anchor)。"""

    def windows(self, anchor):
        return []


class TestRecordingRescue(unittest.TestCase):
    """「判定在播、却不在录制」必须被看护循环救回（2026-09-19 补）。

    缺陷本体：`_give_up()` → `_stop_recording()` 会把主播从 `_recordings` 里摘掉，
    而看护循环原先只遍历 `_recordings` ⇒ 冷却到期后**没有任何机制主动重启录制**，
    只能等下一次检测，而 live_interval=1200s ⇒ 实测空窗 28.5 分钟。
    这一组守住修复：异常态触发一次**提前复检**（检测循环 0.5s 内消费刷新请求），
    由检测循环带着**新流地址**重启录制。
    """

    def setUp(self):
        self.cfg = {
            "recorder": {"enabled": True, "stall_seconds": 90, "stall_max_restarts": 4,
                         "stall_cooldown": 600},
            "monitors": [{"name": "主播甲", "auto_record": True},
                         {"name": "主播乙", "auto_record": True},
                         {"name": "主播丙", "auto_record": False}],
        }
        self.state = FakeState(self.cfg)
        monitor._recordings = {}
        monitor._rec_health = {}
        monitor._rec_giveup_until = {}
        monitor._rec_giveup_reason = {}
        monitor._anomaly_probe_at = {}
        monitor._manual_paused = set()

    def tearDown(self):
        monitor._recordings = {}
        monitor._rec_health = {}
        monitor._rec_giveup_until = {}
        monitor._rec_giveup_reason = {}
        monitor._anomaly_probe_at = {}
        monitor._manual_paused = set()

    def _live(self, name="主播甲", cooldown=None, reason="", **extra):
        """让该主播「判定在播」（检测口径）。

        cooldown 秒数同时写进内部权威字典与快照字段（快照就是由前者派生的），
        保持与真实运行时一致。
        """
        if cooldown:
            monitor._rec_giveup_until[name] = time.time() + cooldown
            monitor._rec_giveup_reason[name] = reason
        snap = {"is_live": True, "phase": "直播中",
                "rec_paused_until": (time.time() + cooldown) if cooldown else None,
                "rec_pause_reason": reason if cooldown else ""}
        snap.update(extra)
        self.state.set_streamer(name, snap)

    # ── 主路径：在播却没在录 → 请求提前复检 ──
    def test_live_without_recording_is_rescued(self):
        self._live()
        monitor._rescue_missing_recordings(self.state, self.cfg)
        self.assertEqual(self.state.refreshed, ["主播甲"])

    def test_offline_is_not_rescued(self):
        self.state.set_streamer("主播甲", {"is_live": False})
        monitor._rescue_missing_recordings(self.state, self.cfg)
        self.assertEqual(self.state.refreshed, [])

    def test_already_recording_is_not_rescued(self):
        self._live()
        monitor._recordings["主播甲"] = {"proc": FakeProc(), "output_path": "/x"}
        monitor._rescue_missing_recordings(self.state, self.cfg)
        self.assertEqual(self.state.refreshed, [])

    def test_manual_paused_is_not_rescued(self):
        """用户手动停过的，不自动开回去（不能违背用户意图）。"""
        self._live()
        monitor._manual_paused.add("主播甲")
        monitor._rescue_missing_recordings(self.state, self.cfg)
        self.assertEqual(self.state.refreshed, [])

    def test_auto_record_off_is_not_rescued(self):
        """关了自动录制的主播只检测不录。"""
        self._live("主播丙")
        monitor._rescue_missing_recordings(self.state, self.cfg)
        self.assertEqual(self.state.refreshed, [])

    # ── 冷却期：期内不折腾，到期立刻救 ──
    def test_cooldown_blocks_rescue(self):
        """冷却期内不折腾（止损设计），等它到期自然会走 test_cooldown_expiry_triggers_rescue。"""
        self._live(cooldown=300, reason="ffmpeg 反复退出")
        monitor._rescue_missing_recordings(self.state, self.cfg)
        self.assertEqual(self.state.refreshed, [])
        self.assertNotIn("主播甲", monitor._anomaly_probe_at)   # 没请求就不该占节流位

    def test_cooldown_expiry_triggers_rescue(self):
        """★ 核心回归：冷却一到期就必须被救回，而不是干等下一次检测。"""
        monitor._rec_giveup_until["主播甲"] = time.time() - 1
        self._live()
        monitor._rescue_missing_recordings(self.state, self.cfg)
        self.assertEqual(self.state.refreshed, ["主播甲"])

    # ── 节流：别把 20s 巡检变成请求风暴 ──
    def test_rescue_is_throttled(self):
        self._live()
        monitor._rescue_missing_recordings(self.state, self.cfg)
        monitor._rescue_missing_recordings(self.state, self.cfg)
        monitor._rescue_missing_recordings(self.state, self.cfg)
        self.assertEqual(self.state.refreshed, ["主播甲"])

    def test_throttle_window_expires(self):
        self._live()
        monitor._rescue_missing_recordings(self.state, self.cfg)
        monitor._anomaly_probe_at["主播甲"] = time.time() - monitor._ANOMALY_RECHECK_SEC - 1
        monitor._rescue_missing_recordings(self.state, self.cfg)
        self.assertEqual(self.state.refreshed, ["主播甲", "主播甲"])

    def test_throttle_resets_on_new_giveup(self):
        """新一轮冷却开始时重新计时，保证到期那一轮能立刻被救。"""
        self._live()
        monitor._anomaly_probe_at["主播甲"] = time.time()
        monitor._give_up(self.state, "主播甲", 600, "反复退出")
        self.assertNotIn("主播甲", monitor._anomaly_probe_at)

    # ── 与看护循环的接线（真跑一轮，确认不是死代码） ──
    def test_watchdog_actually_calls_rescue(self):
        self._live()
        loop = asyncio.new_event_loop()
        try:
            async def once():
                task = asyncio.ensure_future(monitor._recording_watchdog(self.state, interval=1))
                await asyncio.sleep(1.6)       # 一轮巡检(先 sleep(interval) 再干活)
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
            loop.run_until_complete(once())
        finally:
            loop.close()
        self.assertEqual(self.state.refreshed, ["主播甲"])

    # ── 状态暴露：界面要靠它说清「为什么没在录」 ──
    def test_snap_exposes_cooldown_when_paused(self):
        monitor._rec_giveup_until["主播甲"] = time.time() + 300
        monitor._rec_giveup_reason["主播甲"] = "ffmpeg 反复退出,已重启 4 次"
        snap = monitor._snap("主播甲", "anchor", "直播中", FakeStore(), time.time(),
                             120, {"is_live": True})
        self.assertGreater(snap["rec_paused_until"], time.time())
        self.assertIn("反复退出", snap["rec_pause_reason"])

    def test_snap_hides_stale_cooldown(self):
        """冷却已过期就不该再显示暂停（否则界面会永远挂着旧原因）。"""
        monitor._rec_giveup_until["主播甲"] = time.time() - 1
        monitor._rec_giveup_reason["主播甲"] = "旧原因"
        snap = monitor._snap("主播甲", "anchor", "直播中", FakeStore(), time.time(),
                             120, {"is_live": True})
        self.assertIsNone(snap["rec_paused_until"])
        self.assertEqual(snap["rec_pause_reason"], "")

    def test_clear_giveup_clears_reason_too(self):
        monitor._rec_giveup_until["主播甲"] = time.time() + 300
        monitor._rec_giveup_reason["主播甲"] = "原因"
        monitor._clear_giveup("主播甲")
        self.assertNotIn("主播甲", monitor._rec_giveup_until)
        self.assertNotIn("主播甲", monitor._rec_giveup_reason)

    def test_give_up_records_reason(self):
        """放弃录制时要把「为什么」记下来，界面才有话可说。"""
        orig_stop = monitor._stop_recording
        monitor._stop_recording = lambda st, n: None
        try:
            monitor._give_up(self.state, "主播甲", 600, "ffmpeg 反复退出,已重启 4 次")
        finally:
            monitor._stop_recording = orig_stop
        self.assertIn("反复退出", monitor._rec_giveup_reason["主播甲"])
        self.assertGreater(monitor._rec_giveup_until["主播甲"], time.time())
        self.assertAlmostEqual(monitor._rec_giveup_until["主播甲"] - time.time(),
                               600, delta=5)


class TestWebRidPlumbing(unittest.TestCase):
    """矛盾校验必须拿 web_rid 去问 API（2026-10-13 补）。

    缺陷本体：`check_api` 的参数名是 `web_rid`，但调用方把 reflow 页里的 19 位
    `room_id` 传了进去 ⇒ 抖音回 HTTP 200 + status_code=4001038 + 空 data，
    表现得像「房间不存在」。实测某主播三次矛盾校验全部假失败，一个假阳性直播态
    连续空转约 3 小时没人纠正。这一组守住「缺 web_rid 就明确放弃」。
    """

    def setUp(self):
        self.orig_log = monitor.log
        monitor.log = lambda msg: None      # 不污染真实日志
        self.cfg = {"monitors": [{"name": "甲", "web_rid": "36000000000"},
                                 {"name": "乙", "web_rid": ""}]}

    def tearDown(self):
        monitor.log = self.orig_log

    def test_web_rid_of_finds_monitor_entry(self):
        self.assertEqual(monitor._web_rid_of(self.cfg, "甲"), "36000000000")

    def test_web_rid_of_missing_entry(self):
        """没填直播间号的主播：返回空串，由调用方决定怎么退让。"""
        self.assertEqual(monitor._web_rid_of(self.cfg, "乙"), "")
        self.assertEqual(monitor._web_rid_of(self.cfg, "不存在的主播"), "")

    def test_conflict_probe_skips_without_web_rid(self):
        """没有 web_rid 时不许发起 API 校验 —— 那只会再骗一次自己。"""
        calls = []

        async def fake_confirm(*a, **k):
            calls.append(k)
            return None, "ok"

        orig = monitor.confirm_with_cookie    # monitor 顶层 from-import,要打在这里
        monitor.confirm_with_cookie = fake_confirm
        try:
            asyncio.run(monitor._conflict_probe(None, "乙", self.cfg,
                                                "7695832358775212800"))
        finally:
            monitor.confirm_with_cookie = orig
        self.assertEqual(calls, [], "缺 web_rid 时不应发起 Cookie 校验")

    def test_conflict_probe_passes_web_rid(self):
        """有 web_rid 时必须透传给 confirm_with_cookie，而不是退回 room_id。"""
        calls = []

        async def fake_confirm(client, room_id, cfg, **k):
            calls.append((room_id, k.get("web_rid")))
            return None, "ok"

        orig = monitor.confirm_with_cookie    # monitor 顶层 from-import,要打在这里
        monitor.confirm_with_cookie = fake_confirm
        try:
            asyncio.run(monitor._conflict_probe(None, "甲", self.cfg,
                                                "7695832358775212800"))
        finally:
            monitor.confirm_with_cookie = orig
        self.assertEqual(calls, [("7695832358775212800", "36000000000")])


class TestStreamAwait(unittest.TestCase):
    """流侧退出 ≠ 本地故障:不烧重启预算、退避重连、窗满才升级(2026-10-31)。

    数据依据(8 天日志 + 500 场历史,见《断流与下播判定-数据研究与报错逻辑重构》):
      · 404 之后抖音中位 24s、典型 67s 才把状态翻成「已结束」;
      · 61 次「放弃并冷却」里 18 次在 603~621s(冷却刚满)就恢复,而 2~5 分钟
        区间 **0 次** —— 冷却闸门根本不让试,那 10 分钟是白扔的;
      · 262 次重启里 261 次一次即恢复,所以第一跳必须仍然立刻重连。
    """

    DEAD = ("http://pull-flv-h95.douyincdn.com/stage/stream-1_or4.flv"
            "?unique_id=stream-1_857_flv_or4&sign=aaa&expire=1793938508")
    RESIGNED = ("http://pull-flv-h95.douyincdn.com/stage/stream-1_or4.flv"
                "?unique_id=stream-1_857_flv_or4&sign=bbb&expire=1793999999")
    ERR_404 = ("[in#0 @ 0x1] Error opening input: Server returned 404 Not Found\n"
               "Error opening input files: Server returned 404 Not Found\n")

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = self.tmp.name
        self.rec = os.path.join(self.base, "recordings")
        os.makedirs(self.rec, exist_ok=True)
        self.err = os.path.join(self.base, "x.err.log")
        with open(self.err, "w", encoding="utf-8") as f:
            f.write(self.ERR_404)
        self.cfg = {"recorder": {"output_dir": self.rec, "enabled": True,
                                 "stall_seconds": 90, "stall_max_restarts": 3,
                                 "stall_cooldown": 600,
                                 "stream_await_seconds": 900},
                    "danmaku": {"enabled": False}, "nas": {"enabled": False},
                    "archive": {"mode": "auto", "external_dir": ""}}
        self.state = FakeState(self.cfg)
        self.started = []
        self._orig = (monitor._start_recording, monitor._history_finish)
        monitor._start_recording = self._fake_start
        monitor._history_finish = lambda state, name: None
        monitor._recordings = {}
        monitor._rec_health = {}
        monitor._rec_stream_wait = {}
        monitor._rec_giveup_until = {}
        monitor._rec_giveup_reason = {}
        self.addCleanup(self._restore)

    def _restore(self):
        monitor._start_recording, monitor._history_finish = self._orig
        monitor._recordings = {}
        monitor._rec_health = {}
        monitor._rec_stream_wait = {}
        monitor._rec_giveup_until = {}
        monitor._rec_giveup_reason = {}
        self.tmp.cleanup()

    def _fake_start(self, state, name, url, cfg, room_id=None):
        self.started.append(url)
        out = os.path.join(self.rec, name, DAY, f"{name}-%03d.flv")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        monitor._recordings[name] = {
            "proc": FakeProc(), "started_at": time.time(), "output_path": out,
            "stream_url": url, "format": "flv", "room_id": room_id,
            "err_path": self.err}

    def _seed_dead(self):
        """造一个「刚才流侧退出」的场次:进程已退出,err.log 是 404。"""
        out = os.path.join(self.rec, "主播A", DAY, "主播A-%03d.flv")
        os.makedirs(os.path.dirname(out), exist_ok=True)
        proc = FakeProc()
        proc.alive = False
        monitor._recordings["主播A"] = {
            "proc": proc, "started_at": time.time(), "output_path": out,
            "stream_url": self.DEAD, "format": "flv", "room_id": "100",
            "err_path": self.err}
        monitor._rec_health["主播A"] = {"size": 0, "ts": time.time(),
                                        "restarts": 0, "room_id": "100",
                                        "stalled": False}

    def _kill_current(self):
        rec = monitor._recordings.get("主播A")
        if rec:
            rec["proc"].alive = False

    def test_stream_exit_restarts_immediately_without_counting(self):
        """第一跳仍要立刻重连(261/262 次断流就是这一下恢复的),但不计预算。"""
        self._seed_dead()
        monitor._refresh_recording(self.state, "主播A", self.DEAD, self.cfg, "100")
        self.assertEqual(len(self.started), 1)
        self.assertEqual(monitor._rec_health["主播A"]["restarts"], 0,
                         "流侧退出不该消耗本地故障的重启预算")
        self.assertIn("主播A", monitor._rec_stream_wait)
        self.assertIn("主播A", self.state.refreshed,
                      "必须请检测循环尽快判下播,否则会把正常下播拖成 15 分钟后的故障")

    def test_same_address_repeats_do_not_burn_budget(self):
        """同一条死地址反复失败:窗内不许放弃、不许吃 restarts。"""
        self._seed_dead()
        for _ in range(6):
            monitor._refresh_recording(self.state, "主播A", self.DEAD, self.cfg, "100")
            self._kill_current()
        self.assertNotIn("主播A", monitor._rec_giveup_until, "等流窗内不许放弃本场")
        self.assertNotIn("主播A", monitor._rec_giveup_reason)
        h = monitor._rec_health.get("主播A") or {}
        self.assertEqual(int(h.get("restarts") or 0), 0)

    def test_resigned_address_retries_immediately(self):
        """地址指纹变了(重签)= 大概率推流已恢复,退避中也要立刻试。"""
        self._seed_dead()
        monitor._refresh_recording(self.state, "主播A", self.DEAD, self.cfg, "100")
        monitor._rec_stream_wait["主播A"]["next_at"] = time.time() + 300
        self._kill_current()
        before = len(self.started)
        monitor._refresh_recording(self.state, "主播A", self.RESIGNED, self.cfg, "100")
        self.assertEqual(len(self.started), before + 1, "换了新地址必须立刻重连")
        self.assertEqual(monitor._rec_stream_wait["主播A"]["fp"],
                         monitor._url_fingerprint(self.RESIGNED))

    def test_backoff_skips_round_without_giving_up(self):
        """退避中就安静等一轮:既不重开空文件,也不放弃。"""
        self._seed_dead()
        monitor._refresh_recording(self.state, "主播A", self.DEAD, self.cfg, "100")
        self._kill_current()
        monitor._rec_stream_wait["主播A"]["next_at"] = time.time() + 300
        before = len(self.started)
        monitor._refresh_recording(self.state, "主播A", self.DEAD, self.cfg, "100")
        self.assertEqual(len(self.started), before, "退避期内不该再开一个空文件")
        self.assertNotIn("主播A", monitor._rec_giveup_until)

    def test_await_window_expiry_gives_up(self):
        """窗满、抖音仍判在播、地址仍不变 → 这才升级成故障。"""
        self._seed_dead()
        monitor._refresh_recording(self.state, "主播A", self.DEAD, self.cfg, "100")
        monitor._rec_stream_wait["主播A"]["since"] = time.time() - 1000
        self._kill_current()
        monitor._refresh_recording(self.state, "主播A", self.DEAD, self.cfg, "100")
        self.assertIn("主播A", monitor._rec_giveup_until, "窗满仍未恢复应放弃本场")
        self.assertNotIn("主播A", monitor._rec_stream_wait)

    def test_offline_clears_await_state(self):
        """检测循环判下播 → _stop_recording → 等流态就地清空(正常下播不报错)。"""
        self._seed_dead()
        monitor._refresh_recording(self.state, "主播A", self.DEAD, self.cfg, "100")
        self.assertIn("主播A", monitor._rec_stream_wait)
        monitor._stop_recording(self.state, "主播A")
        self.assertNotIn("主播A", monitor._rec_stream_wait)
        self.assertNotIn("主播A", monitor._rec_giveup_until)

    def test_recovery_clears_await_state(self):
        """数据在长 = episode 结束,必须清账。

        不清的后果很硬:同一个 stream key 在一场里可以用几个小时,第二次断流的
        指纹与第一次相同,而 `since` 还是第一次的时刻 —— 于是「已等 3 小时」
        直接顶穿等流窗,把一次普通抖动判成本场放弃。
        """
        self._seed_dead()
        monitor._refresh_recording(self.state, "主播A", self.DEAD, self.cfg, "100")
        monitor._rec_stream_wait["主播A"]["since"] = time.time() - 5000
        rec = monitor._recordings["主播A"]
        rec["proc"].alive = True
        with open(os.path.join(os.path.dirname(rec["output_path"]),
                               "主播A-000.flv"), "ab") as f:
            f.write(b"x" * 500)
        monitor._rec_health["主播A"].update({"size": 0, "ts": time.time()})
        monitor._refresh_recording(self.state, "主播A", self.DEAD, self.cfg, "100")
        self.assertNotIn("主播A", monitor._rec_stream_wait, "恢复后必须清掉等流态")
        self.assertNotIn("主播A", monitor._rec_giveup_until)

    def test_url_fingerprint_tracks_signature(self):
        self.assertEqual(monitor._url_fingerprint(self.DEAD),
                         monitor._url_fingerprint(self.DEAD))
        self.assertNotEqual(monitor._url_fingerprint(self.DEAD),
                            monitor._url_fingerprint(self.RESIGNED),
                            "重签后的地址必须认成新地址")
        self.assertEqual(monitor._url_fingerprint(""), "")
        self.assertEqual(monitor._url_fingerprint(None), "")
