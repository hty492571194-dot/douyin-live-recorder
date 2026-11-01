# -*- coding: utf-8 -*-
"""下播/断流后的残片回收 + 历史页分段显示。

成因(2026-09-21 排查):房间还挂着「直播中」但推流已被 CDN 切断,复检判在播 →
起录 → 约 60 秒流死退出(Stream ends prematurely)→ 重启 4 次 → give_up;
每次重启都 _history_finish + _history_add ⇒ 一场失败循环留下 4-5 条
~1 分钟的历史条目和几十 MB 的残片文件。用户看到的就是
「历史里总有多出 20 秒的视频残留」。
"""
import os
import sys
import tempfile
import time
import unittest

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
sys.path.insert(0, os.path.join(BASE, "scripts"))

import monitor  # noqa: E402

with open(os.path.join(BASE, "static", "index.html"), encoding="utf-8") as _f:
    HTML = _f.read()

PAST = 1_700_000_000.0          # 固定的时间基准,让判据可预测


class FakeState:
    def __init__(self):
        self.recordings = {}
        self.refreshed = []

    def get_config(self):
        return {}

    def set_recording(self, name, info):
        self.recordings[name] = info

    def remove_recording(self, name):
        self.recordings.pop(name, None)

    def request_refresh(self, name):
        self.refreshed.append(name)


def _commit_calls(monkey):
    """拦截 _commit_history,避免测试写真实 history.json。"""
    calls = {"n": 0}
    monkey["orig"] = monitor._commit_history
    monitor._commit_history = lambda: calls.__setitem__("n", calls["n"] + 1)
    return calls


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = self.tmp.name
        self.state = FakeState()
        self.mon = {}
        self._orig = dict(
            history=[], carry={}, session={}, recordings={}, health={},
            giveup={}, giveup_reason={}, anomaly={})
        monitor._history = []
        monitor._rec_carry = {}
        monitor._session_start = {}
        monitor._recordings = {}
        monitor._rec_health = {}
        monitor._rec_giveup_until = {}
        monitor._rec_giveup_reason = {}
        monitor._anomaly_probe_at = {}
        self.calls = _commit_calls(self.mon)

    def tearDown(self):
        monitor._commit_history = self.mon["orig"]
        monitor._history = []
        monitor._rec_carry = {}
        monitor._session_start = {}
        monitor._recordings = {}
        monitor._rec_health = {}
        monitor._rec_giveup_until = {}
        monitor._rec_giveup_reason = {}
        monitor._anomaly_probe_at = {}
        self.tmp.cleanup()

    # ── 夹具 ──
    def _write_flv(self, name, size):
        p = os.path.join(self.d, name)
        with open(p, "wb") as f:
            f.write(b"x" * size)
        return p

    def _history_entry(self, name="主播甲", stem="主播甲-20260921-120000",
                       dur=60.0, n_seg=1, seg_size=12_000_000, status="done",
                       started=None):
        """造一条 done 条目 + 对应的分片文件(n_seg 个,每个 seg_size 字节)。"""
        for i in range(n_seg):
            self._write_flv("%s-%03d.flv" % (stem, i), seg_size)
        e = {"name": name, "started_at": started or PAST,
             "output_path": os.path.join(self.d, stem + "-%03d.flv"),
             "status": status, "duration": dur,
             "size": seg_size * n_seg if status == "done" else None,
             "archive": {"state": "none", "target": None,
                         "dest": None, "at": None, "error": None}}
        monitor._history.append(e)
        return e


class TestJunkDiscard(_Base):
    """give_up 后,本场残渣条目与文件一并回收。"""

    def test_short_entries_and_files_removed(self):
        """3 条 60s 短条目(流死循环的典型形态)→ 全删,文件也没了。"""
        self._history_entry(stem="主播甲-20260921-120000", dur=60.0, n_seg=1)
        self._history_entry(stem="主播甲-20260921-120100", dur=55.0, n_seg=1)
        self._history_entry(stem="主播甲-20260921-120200", dur=81.0, n_seg=1)
        monitor._session_start["主播甲"] = PAST
        monitor._discard_junk_segments(self.state, "主播甲")

        self.assertEqual(len(monitor._history), 0)
        self.assertEqual(os.listdir(self.d), [], "残片文件没删干净")
        self.assertGreaterEqual(self.calls["n"], 1)

    def test_long_entry_survives(self):
        """停滞场景:本场有一条 30 分钟的正常条目 → 保留;60s 残渣 → 删。"""
        self._history_entry(stem="主播甲-20260921-120000", dur=1800.0, n_seg=2)
        self._history_entry(stem="主播甲-20260921-125000", dur=60.0, n_seg=1)
        monitor._session_start["主播甲"] = PAST
        monitor._discard_junk_segments(self.state, "主播甲")

        self.assertEqual(len(monitor._history), 1)
        self.assertEqual(monitor._history[0]["duration"], 1800.0)
        self.assertTrue(os.path.exists(os.path.join(
            self.d, "主播甲-20260921-120000-000.flv")), "正常内容被误删了")
        self.assertFalse(os.path.exists(os.path.join(
            self.d, "主播甲-20260921-125000-000.flv")))

    def test_tiny_file_caught_by_size_even_with_long_wallclock(self):
        """停滞空转条目:墙钟 150s 但文件 0 字节 → 靠体积判据兜住。"""
        self._history_entry(stem="主播甲-20260921-120000", dur=150.0, n_seg=0)
        monitor._session_start["主播甲"] = PAST
        monitor._discard_junk_segments(self.state, "主播甲")
        self.assertEqual(len(monitor._history), 0)

    def test_entry_before_session_start_untouched(self):
        """上一场的长条目(started_at 早于本场起点)不受影响。"""
        self._history_entry(stem="主播甲-20260921-110000", dur=3600.0, n_seg=1,
                            started=PAST - 7200)
        monitor._session_start["主播甲"] = PAST
        monitor._discard_junk_segments(self.state, "主播甲")
        self.assertEqual(len(monitor._history), 1)

    def test_other_streamer_untouched(self):
        self._history_entry(name="主播乙", stem="主播乙-20260921-120000", dur=30.0)
        monitor._session_start["主播甲"] = PAST
        monitor._discard_junk_segments(self.state, "主播甲")
        self.assertEqual(len(monitor._history), 1)

    def test_no_session_start_noop(self):
        """没有本场起点(纯脚本调用/异常态)→ 不删任何东西,宁可漏删不可误删。"""
        self._history_entry(stem="主播甲-20260921-120000", dur=60.0, n_seg=1)
        monitor._discard_junk_segments(self.state, "主播甲")
        self.assertEqual(len(monitor._history), 1)

    def test_ass_and_meta_companions_removed(self):
        """回收要连带 .ass 与 .meta 明细,否则留下孤儿字幕。"""
        e = self._history_entry(stem="主播甲-20260921-120000", dur=60.0, n_seg=1)
        self._write_flv("主播甲-20260921-120000-000.ass", 100)
        meta = os.path.join(self.d, ".meta")
        os.makedirs(meta, exist_ok=True)
        with open(os.path.join(meta, "主播甲-20260921-120000-000.danmaku.jsonl"), "w") as f:
            f.write("{}")
        monitor._session_start["主播甲"] = PAST
        monitor._discard_junk_segments(self.state, "主播甲")
        self.assertEqual(os.listdir(self.d), [".meta"], ".ass 没删")
        self.assertEqual(os.listdir(meta), [], ".meta 明细没删")

    def test_recording_entries_not_touched(self):
        """recording 状态(正在录)的条目不属于回收范围 —— give_up 时最后一条
        已由 _stop_recording → _history_finish 转成 done,这里只是防御。"""
        self._history_entry(stem="主播甲-20260921-120000", dur=60.0, n_seg=1,
                            status="recording")
        monitor._session_start["主播甲"] = PAST
        monitor._discard_junk_segments(self.state, "主播甲")
        self.assertEqual(len(monitor._history), 1)


class TestGiveUpWiring(_Base):
    """give_up 必须触发回收 + 请求复检;session 起点的生命周期要正确。"""

    def test_give_up_discards_and_requests_refresh(self):
        monitor._session_start["主播甲"] = PAST
        monitor._recordings["主播甲"] = {
            "proc": _FakeProc(), "started_at": PAST,
            "output_path": os.path.join(self.d, "主播甲-20260921-120000-%03d.flv"),
            "stream_url": "u", "format": "flv"}
        self._history_entry(stem="主播甲-20260921-120000", dur=60.0, n_seg=1)
        monitor._rec_health["主播甲"] = {"size": 0, "ts": PAST, "restarts": 4}

        monitor._give_up(self.state, "主播甲", 600, "ffmpeg 反复退出,已重启 4 次")

        # 残片被回收(文件 + 条目)
        self.assertEqual(len(monitor._history), 0)
        self.assertEqual(os.listdir(self.d), [])
        # 请求了复检
        self.assertEqual(self.state.refreshed, ["主播甲"])
        # 冷却与原因已登记
        self.assertGreater(monitor._rec_giveup_until["主播甲"], time.time())
        self.assertIn("反复退出", monitor._rec_giveup_reason["主播甲"])
        # 本场起点已清
        self.assertNotIn("主播甲", monitor._session_start)

    def test_stop_recording_clears_session_start(self):
        monitor._session_start["主播甲"] = PAST
        monitor._recordings["主播甲"] = {
            "proc": _FakeProc(), "started_at": PAST,
            "output_path": os.path.join(self.d, "x-%03d.flv"),
            "stream_url": "u", "format": "flv"}
        monitor._stop_recording(self.state, "主播甲")
        self.assertNotIn("主播甲", monitor._session_start)

    def test_session_start_survives_restart(self):
        """restart 是同一场:起点不能被重置(否则回收判定漂移)。"""
        monitor._session_start["主播甲"] = PAST
        monitor._recordings["主播甲"] = {
            "proc": _FakeProc(), "started_at": PAST,
            "output_path": os.path.join(self.d, "a-%03d.flv"),
            "stream_url": "u", "format": "flv"}
        monitor._carry_segments("主播甲")
        monitor._recordings.pop("主播甲")
        monitor._history_finish(self.state, "主播甲")
        monitor._start_recording(self.state, "主播甲", "u",
                                 {"recorder": {"enabled": True}}, room_id="r")
        self.assertEqual(monitor._session_start["主播甲"], PAST)

    def test_start_recording_records_session_start(self):
        monitor._start_recording(self.state, "主播甲", "u",
                                 {"recorder": {"enabled": True}}, room_id="r")
        self.assertIn("主播甲", monitor._session_start)
        self.assertGreater(monitor._session_start["主播甲"], 0)


class TestHistorySegments(_Base):
    """历史条目要记录段数/真实时长/真实体积。"""

    def test_finish_fills_segments_size_duration(self):
        """给模板路径造真实分片文件 → finish 后 segments/size 有值。"""
        stem = "主播甲-20260921-120000"
        for i in range(3):
            self._write_flv("%s-%03d.flv" % (stem, i), 4096)
        monitor._history.append({
            "name": "主播甲", "started_at": PAST,
            "output_path": os.path.join(self.d, stem + "-%03d.flv"),
            "status": "recording",
            "archive": {"state": "none"}})
        monitor._history_finish(self.state, "主播甲", ended_at=PAST + 7000)
        e = monitor._history[0]
        self.assertEqual(e["segments"], 3)
        self.assertEqual(e["size"], 4096 * 3)
        # 0 字节的假 flv 解析不出时间戳 → duration 回退墙钟(7000s)
        self.assertEqual(e["duration"], 7000.0)

    def test_media_duration_handles_find_segments_tuples(self):
        """回归:`find_segments` 返回 [(idx, 路径)],取路径而不是整个元组。

        2026-09-21 排查「末段对齐」时发现:模板分支直接把元组传给
        flv_first_av_ts_abs → TypeError → except 吞掉 → 永远回退墙钟。
        测试此前用 0 字节假 flv,恰好也走回退分支,掩盖了这个 bug
        —— "测试通过" ≠ "功能正确" 的又一例。
        """
        import subtitle as S
        real = S.find_segments  # 保留原函数引用
        stem = "主播甲-20260921-120000"
        # 用 monkeypatch 的 find_segments 返回真实形态的 [(idx, path)],
        # 配合 mock flv_first_av_ts_abs / flv_last_ts_abs 验证解包正确
        orig_find = monitor.subtitle_mod.find_segments
        tmpl = os.path.join(self.d, stem + "-%03d.flv")
        calls = []

        def fake_find(op):
            calls.append(op)
            return [("000", os.path.join(self.d, stem + "-000.flv")),
                    ("001", os.path.join(self.d, stem + "-001.flv"))]

        monitor.subtitle_mod.find_segments = fake_find
        # 2026-10-27 起 _media_duration 只取**原始 tag 值**作差(时长与"时间是墙钟
        # 还是源流相对值"无关,也就不必先还原绝对时间),所以改为 mock 这两个。
        orig_raw_a = monitor.subtitle_mod.flv_first_av_ts
        orig_raw_b = monitor.subtitle_mod.flv_last_ts
        monitor.subtitle_mod.flv_first_av_ts = lambda p: (
            calls.append("first:" + os.path.basename(p)) or 1000)
        monitor.subtitle_mod.flv_last_ts = lambda p: (
            calls.append("last:" + os.path.basename(p)) or 65000)
        try:
            dur = monitor._media_duration(tmpl)
        finally:
            monitor.subtitle_mod.find_segments = orig_find
            monitor.subtitle_mod.flv_first_av_ts = orig_raw_a
            monitor.subtitle_mod.flv_last_ts = orig_raw_b
        self.assertEqual(dur, 64.0)          # (65000-1000)/1000
        # 传入 flv_* 的是路径字符串,不是元组
        self.assertIn("first:" + stem + "-000.flv", calls)
        self.assertIn("last:" + stem + "-001.flv", calls)
        self.assertFalse(any(isinstance(c, tuple) for c in calls))

    def test_single_file_entry_no_segments(self):
        p = self._write_flv("主播甲-20260921-120000.flv", 4096)
        monitor._history.append({
            "name": "主播甲", "started_at": PAST, "output_path": p,
            "status": "recording", "archive": {"state": "none"}})
        monitor._history_finish(self.state, "主播甲", ended_at=PAST + 60)
        e = monitor._history[0]
        self.assertNotIn("segments", e)
        self.assertEqual(e["size"], 4096)


class TestHistoryUi(unittest.TestCase):
    """历史页要显示「N 段」。"""

    def test_consumes_segments(self):
        self.assertIn("e.segments", HTML)
        self.assertIn("segPill", HTML, "段数进胶囊(.h-pill)")

    def test_hidden_while_recording_or_single_file(self):
        i = HTML.index("const segPill")
        snippet = HTML[i:i + 260]
        self.assertIn("!recording", snippet)
        self.assertIn("e.segments > 0", snippet)

    def test_backend_writes_segments(self):
        src = open(os.path.join(BASE, "monitor.py"), encoding="utf-8").read()
        self.assertIn('e["segments"]', src)


class _FakeProc:
    def terminate(self):
        pass

    def wait(self, timeout=None):
        return 0

    def kill(self):
        pass

    def poll(self):
        return 0


if __name__ == "__main__":
    unittest.main()


class TestDeferredHistory(_Base):
    """延迟入史:404 秒退的尝试不该产生历史条目(用户看到的「20s 残留」本体)。"""

    def _start(self, name="主播甲", stem="主播甲-20260921-120000"):
        p = os.path.join(self.d, stem + "-%03d.flv")
        monitor._recordings[name] = {
            "proc": _FakeProc(), "started_at": PAST, "output_path": p,
            "stream_url": "u", "format": "flv", "history_pending": True}
        monitor._session_start.setdefault(name, PAST)
        return name

    def test_alive_after_first_watchdog_gets_history(self):
        """活过首次巡检 → 补记历史(started_at 是真实起录时间)。"""
        name = self._start()
        rec = monitor._recordings[name]
        # 模拟"活着":poll 返回 None
        class LiveProc:
            def poll(self):
                return None
        rec["proc"] = LiveProc()
        self.assertFalse(any(e.get("name") == name for e in monitor._history),
                         "起录时就不该立即入史")
        # watchdog 的补记逻辑内联在 _recording_watchdog 里,这里复刻其判据
        if rec.pop("history_pending", None):
            if rec["proc"].poll() is None:
                monitor._history_add(self.state, name, rec["output_path"],
                                     rec["started_at"])
        self.assertEqual(len(monitor._history), 1)
        self.assertEqual(monitor._history[0]["started_at"], PAST)
        self.assertEqual(monitor._history[0]["status"], "recording")

    def test_died_before_first_watchdog_no_history(self):
        """404 秒退(巡检时已退出)→ 不入史:垃圾条目从源头消失。"""
        name = self._start()
        rec = monitor._recordings[name]     # _FakeProc.poll() 返回 0(已退出)
        if rec.pop("history_pending", None):
            if rec["proc"].poll() is None:
                monitor._history_add(self.state, name, rec["output_path"],
                                     rec["started_at"])
        self.assertEqual(monitor._history, [])
        self.assertNotIn("history_pending", rec)

    def test_start_recording_sets_pending(self):
        """_start_recording 不再立即入史,而是打 history_pending 标记。"""
        monitor._start_recording(self.state, "主播甲", "u",
                                 {"recorder": {"enabled": True}}, room_id="r")
        self.assertTrue(monitor._recordings["主播甲"].get("history_pending"))
        self.assertEqual([e for e in monitor._history if e.get("name") == "主播甲"],
                         [], "起录时就写了 history")


class TestPurgeScript(unittest.TestCase):
    """存量清理脚本的判据:宁可漏删,不可误删真实短直播。"""

    def test_module_imports_and_runs_dryrun(self):
        import subprocess
        r = subprocess.run(
            [os.path.join(BASE, ".venv", "bin", "python"),
             os.path.join(BASE, "scripts", "purge_ghost_history.py")],
            capture_output=True, text=True, cwd=BASE,
            env={**os.environ, "PYTHONPATH": ""})
        self.assertEqual(r.returncode, 0, r.stderr[-300:])
        # 真实 history 可能已清完(无幽灵)也可能还有,两种正常输出都接受
        self.assertTrue("干跑" in r.stdout or "没有需要清理" in r.stdout,
                        r.stdout[:200])

    def test_find_ghosts_keeps_entries_with_files(self):
        sys.path.insert(0, os.path.join(BASE, "scripts"))
        import purge_ghost_history as pg
        with tempfile.TemporaryDirectory() as d:
            # 短条目但文件在 → 保留
            p = os.path.join(d, "a-20260921-120000-%03d.flv")
            with open(p.replace("%03d", "000"), "wb") as f:
                f.write(b"x" * 100)
            hist = [{"name": "a", "status": "done", "started_at": PAST,
                     "output_path": p, "duration": 20.0, "size": None}]
            ghosts, keep = pg.find_ghosts(hist)
            self.assertEqual(len(ghosts), 0)
            self.assertEqual(len(keep), 1)

    def test_find_ghosts_flags_true_ghost(self):
        sys.path.insert(0, os.path.join(BASE, "scripts"))
        import purge_ghost_history as pg
        with tempfile.TemporaryDirectory() as d:
            hist = [{"name": "a", "status": "done", "started_at": PAST,
                     "output_path": os.path.join(d, "a-20260921-120000-%03d.flv"),
                     "duration": 20.0, "size": None,
                     "archive": {"state": "archived", "dest": os.path.join(d, "nope")}}]
            ghosts, keep = pg.find_ghosts(hist)
            self.assertEqual(len(ghosts), 1)
            self.assertEqual(keep, [])

    def test_find_ghosts_keeps_normal_duration(self):
        sys.path.insert(0, os.path.join(BASE, "scripts"))
        import purge_ghost_history as pg
        hist = [{"name": "a", "status": "done", "started_at": PAST,
                 "output_path": "/nonexistent/x-%03d.flv",
                 "duration": 3600.0, "size": None}]
        ghosts, keep = pg.find_ghosts(hist)
        self.assertEqual((len(ghosts), len(keep)), (0, 1))
