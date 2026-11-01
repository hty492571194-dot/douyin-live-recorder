#!/usr/bin/env python3
"""实时预览(按需独立拉流)的单元测试。

架构(2026-10-11 用户决策「回退」后定稿):录制恢复单路输出,与预览完全解耦;
预览改为「打开页面才启动」的独立 ffmpeg(第二路 CDN 连接),页面关闭后空闲回收。
覆盖四块:
  ① 录制参数回退保证 —— _build_ffmpeg_args 只有一路输出,不残留双输出痕迹;
  ② live_preview 会话管理 —— 启动/上限/续期/空闲回收/下线宽限/退出退避重试;
  ③ live_hls 目录管理 —— 准备/清理/防路径穿越/就绪判定;
  ④ danmaku 环形缓冲 + HTTP 分发层(含新增 POST /api/live/<名>/start)。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import danmaku  # noqa: E402
import live_hls  # noqa: E402
import live_preview  # noqa: E402
import monitor  # noqa: E402


def args_for(rec_cfg):
    args, _ = monitor._build_ffmpeg_args(
        "/usr/bin/ffmpeg", "http://example/stream.flv", "/tmp/out",
        "主播甲", "flv", rec_cfg, "20261011-120000")
    return args


class TestBuildArgsSingleOutput(unittest.TestCase):
    """回退保证:录制 ffmpeg 只有一路输出,不残留双输出痕迹。"""

    def test_no_second_output(self):
        a = args_for({"segment_time": 2300, "bitrate": 1900})
        self.assertNotIn("hls", a)
        self.assertNotIn("tee", a)
        self.assertNotIn("-map", a, "录制命令里不应有第二路输出的 -map")
        i = a.index("/tmp/out/主播甲-20261011-120000-%03d.flv")
        self.assertEqual(a[i + 1:], [], "分片输出之后不应再有任何参数")

    def test_copy_mode_single_file(self):
        a = args_for({"segment_time": 0, "bitrate": 0})
        self.assertEqual(a[-1], "/tmp/out/主播甲-20261011-120000.flv")
        self.assertIn("copy", a)
        self.assertNotIn("hls", a)


class _FakeProc:
    """假 ffmpeg 进程:手动置退出码,记录 terminate/kill。"""
    _n = 0

    def __init__(self):
        _FakeProc._n += 1
        self.pid = 40000 + _FakeProc._n
        self._code = None
        self.terminated = 0
        self.killed = 0

    def poll(self):
        return self._code

    def exit(self, code=0):
        self._code = code

    def terminate(self):
        self.terminated += 1
        if self._code is None:
            self._code = -15

    def kill(self):
        self.killed += 1
        self._code = -9

    def wait(self, timeout=None):
        return self._code


class TestLivePreviewSession(unittest.TestCase):
    """会话管理:假 launcher 驱动全生命周期(不真起 ffmpeg)。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = {"live_preview": {"enabled": True, "dir": self.tmp,
                                     "idle_timeout": 30, "max_sessions": 2}}
        self.launched = []
        self.procs = []
        self.url = "http://fake/stream.flv"
        live_preview._sessions.clear()
        live_preview._reaper_started = True     # 测试不拉后台线程
        live_preview._last_cfg = self.cfg
        live_preview.set_url_provider(lambda n: self.url)
        self._orig_launch = live_preview._launch
        self._orig_find = live_preview.find_ffmpeg

        def fake_launch(args, err_path):
            p = _FakeProc()
            self.procs.append(p)
            self.launched.append(list(args))
            return p

        live_preview._launch = fake_launch
        live_preview.find_ffmpeg = lambda: "/usr/bin/ffmpeg"

    def tearDown(self):
        live_preview.stop_all(self.cfg)
        live_preview._launch = self._orig_launch
        live_preview.find_ffmpeg = self._orig_find
        live_preview.set_url_provider(None)
        live_preview._sessions.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_start_spawns_and_cleans_old(self):
        d = live_hls.prepare(self.cfg, "主播A")
        open(os.path.join(d, "seg-00001.ts"), "w").write("old")
        res = live_preview.ensure_start(self.cfg, "主播A")
        self.assertTrue(res["ok"], res)
        self.assertEqual(len(self.launched), 1)
        cmd = self.launched[0]
        self.assertIn("copy", cmd)
        self.assertEqual(cmd[cmd.index("-f") + 1], "hls")
        self.assertIn("delete_segments+omit_endlist",
                      cmd[cmd.index("-hls_flags") + 1])
        self.assertEqual(cmd[cmd.index("-hls_segment_filename") + 1],
                         os.path.join(d, "seg-%05d.ts"))
        self.assertIn("-use_wallclock_as_timestamps", cmd)
        self.assertFalse(os.path.exists(os.path.join(d, "seg-00001.ts")),
                         "启动前的旧分片应被清掉")

    def test_cap_rejects(self):
        self.assertTrue(live_preview.ensure_start(self.cfg, "A")["ok"])
        self.assertTrue(live_preview.ensure_start(self.cfg, "B")["ok"])
        res = live_preview.ensure_start(self.cfg, "C")
        self.assertFalse(res["ok"])
        self.assertIn("上限", res["error"])

    def test_idle_reap(self):
        live_preview.ensure_start(self.cfg, "A")
        t0 = time.monotonic()
        live_preview.touch("A")
        live_preview._reap_once(now=t0 + 10)
        self.assertIn("A", live_preview._sessions, "10s 未到空闲线,不应回收")
        live_preview._reap_once(now=t0 + 31)
        self.assertNotIn("A", live_preview._sessions)
        self.assertIsNotNone(self.procs[0].poll(), "空闲回收应终止 ffmpeg")
        d = live_hls.streamer_dir(self.cfg, "A")
        left = [f for f in os.listdir(d) if f.endswith((".ts", ".m3u8"))]
        self.assertEqual(left, [], "回收时应清掉预览分片")

    def test_provider_none_grace(self):
        live_preview.ensure_start(self.cfg, "A")
        self.url = None
        t0 = time.monotonic()
        live_preview._reap_once(now=t0 + 6)
        self.assertIn("A", live_preview._sessions, "宽限期内不应结束(录制重启空窗)")
        live_preview.touch("A")
        live_preview._reap_once(now=t0 + 27)
        self.assertNotIn("A", live_preview._sessions, "持续无源应结束会话")

    def test_exit_retry_then_manual_reset(self):
        live_preview.ensure_start(self.cfg, "A")
        now = time.monotonic()
        self.procs[-1].exit()               # 模拟 ffmpeg 意外退出
        live_preview._reap_once(now=now)
        s = live_preview._sessions["A"]
        self.assertEqual(s["attempt"], 1)
        self.assertIsNotNone(s["retry_at"])
        # 连续失败直到耗尽退避次数 → failed
        for _ in range(len(live_preview.RETRY_DELAYS) + 1):
            live_preview._reap_once(
                now=live_preview._sessions["A"]["retry_at"] + 0.01)
            if live_preview.status(self.cfg, "A")["state"] == "failed":
                break
            self.procs[-1].exit()
            live_preview._reap_once(now=time.monotonic())
        self.assertEqual(live_preview.status(self.cfg, "A")["state"], "failed")
        n = len(self.launched)
        res = live_preview.ensure_start(self.cfg, "A")   # 手动刷新
        self.assertTrue(res["ok"])
        self.assertGreaterEqual(len(self.launched), n + 1, "手动重试应立即重拉")
        self.assertNotEqual(live_preview.status(self.cfg, "A")["state"], "failed")

    def test_status_ready_transition(self):
        self.assertEqual(live_preview.status(self.cfg, "X")["state"], "absent")
        live_preview.ensure_start(self.cfg, "X")
        self.assertEqual(live_preview.status(self.cfg, "X")["state"], "starting")
        d = live_hls.streamer_dir(self.cfg, "X")
        with open(os.path.join(d, "live.m3u8"), "w") as f:
            f.write("#EXTM3U\nseg-00000.ts\n")
        self.assertEqual(live_preview.status(self.cfg, "X")["state"], "ready")

    def test_cfg_sanitized(self):
        v = live_preview._cfg_vals(
            {"live_preview": {"idle_timeout": "abc", "max_sessions": 99}})
        self.assertEqual(v["idle"], 30)
        self.assertEqual(v["max"], 10)
        v2 = live_preview._cfg_vals(
            {"live_preview": {"idle_timeout": 1, "max_sessions": -5}})
        self.assertEqual(v2["idle"], 5)
        self.assertEqual(v2["max"], 1)


class TestLiveHlsFs(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.cfg = {"live_preview": {"dir": self.tmp}}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_prepare_creates_and_cleans(self):
        d = live_hls.streamer_dir(self.cfg, "示例主播🐰")
        os.makedirs(d)
        open(os.path.join(d, "seg-00001.ts"), "w").write("old")
        open(os.path.join(d, "live.m3u8"), "w").write("old")
        open(os.path.join(d, "keepme.txt"), "w").write("keep")
        got = live_hls.prepare(self.cfg, "示例主播🐰")
        self.assertEqual(got, d)
        names = os.listdir(d)
        self.assertNotIn("seg-00001.ts", names)
        self.assertNotIn("live.m3u8", names)
        self.assertIn("keepme.txt", names, "非 HLS 文件不该被清掉")

    def test_cleanup_keeps_dir(self):
        d = live_hls.prepare(self.cfg, "主播 甲")
        open(os.path.join(d, "seg-00000.ts"), "w").write("x")
        live_hls.cleanup(self.cfg, "主播 甲")
        self.assertTrue(os.path.isdir(d))
        self.assertEqual([f for f in os.listdir(d) if f.endswith(".ts")], [])

    def test_safe_join_guards(self):
        d = live_hls.prepare(self.cfg, "主播甲")
        open(os.path.join(d, "live.m3u8"), "w").write("#EXTM3U")
        self.assertIsNotNone(live_hls.safe_join(self.cfg, "主播甲", "live.m3u8"))
        for evil in ("../x.ts", "a/b.ts", "..", "", "x\\y.ts"):
            self.assertIsNone(live_hls.safe_join(self.cfg, "主播甲", evil), repr(evil))

    def test_is_ready(self):
        self.assertFalse(live_hls.is_ready(self.cfg, "主播甲"))
        d = live_hls.prepare(self.cfg, "主播甲")
        open(os.path.join(d, "live.m3u8"), "w").write("#EXTM3U\nseg-00000.ts\n")
        self.assertTrue(live_hls.is_ready(self.cfg, "主播甲"))
        open(os.path.join(d, "live.m3u8"), "w").write("")
        self.assertFalse(live_hls.is_ready(self.cfg, "主播甲"), "空文件不算就绪")

    def test_root_default(self):
        cfg = {"live_preview": {"dir": ""}}
        self.assertEqual(os.path.basename(live_hls.resolve_root(cfg)), "previews-live")


class TestDanmakuFeed(unittest.TestCase):
    """环形缓冲:不连 WS,直接驱动 _write。"""

    def _rec(self, n=0):
        r = danmaku.DanmakuRecorder("测试主播", "1", "/tmp/never.jsonl")
        r._fh = None
        for i in range(n):
            r._write({"ts": i, "type": "chat", "user": "u", "content": "c%d" % i})
        return r

    def test_seq_monotonic_and_filter(self):
        r = self._rec(5)
        seq, items = r.feed(0)
        self.assertEqual(seq, 5)
        self.assertEqual([e["seq"] for e in items], [1, 2, 3, 4, 5])
        seq2, items2 = r.feed(3)
        self.assertEqual([e["seq"] for e in items2], [4, 5])
        seq3, items3 = r.feed(5)
        self.assertEqual(items3, [])
        self.assertEqual(seq3, 5, "空增量时 seq 不能倒退")

    def test_capacity_cap(self):
        r = self._rec(310)
        seq, items = r.feed(0)
        self.assertEqual(len(items), 300)
        self.assertEqual(items[-1]["seq"], 310)
        self.assertEqual(items[0]["seq"], 11, "应保留最近 300 条")

    def test_module_live_feed_no_session(self):
        out = danmaku.live_feed("不存在的会话", 0)
        self.assertEqual(out["active"], False)
        self.assertEqual(out["items"], [])

    def test_module_live_feed_with_session(self):
        r = self._rec(3)
        danmaku._sessions["临时主播"] = {"rec": r, "prefix": "/tmp/x"}
        try:
            out = danmaku.live_feed("临时主播", 1)
            self.assertTrue(out["active"])
            self.assertEqual(out["seq"], 3)
            self.assertEqual([e["seq"] for e in out["items"]], [2, 3])
        finally:
            danmaku._sessions.pop("临时主播", None)


class TestHandlerDispatch(unittest.TestCase):
    """HTTP 分发层冒烟:真起一个 ThreadingHTTPServer 走真 socket。

    诞生背景:改动路由时误删了 `def do_POST(self):`,755 项既有测试全绿
    也没发现(POST 全接口失联) —— 这一层必须有测试兜住。
    """

    @classmethod
    def setUpClass(cls):
        import webui
        cls.tmp = tempfile.mkdtemp()
        cfg_path = os.path.join(cls.tmp, "config.json")
        cfg = {"webui": {"host": "127.0.0.1", "port": 0}, "monitors": [],
               "live_preview": {"enabled": True}}
        with open(cfg_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f)
        cls.state = webui.State(cfg_path, cfg, None)
        cls.state.set_history([])
        cls.srv, _ = webui.start(cls.state, "127.0.0.1", 0)
        cls.port = cls.srv.server_address[1]
        cls.base = "http://127.0.0.1:%d" % cls.port
        live_preview._sessions.clear()

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        shutil.rmtree(cls.tmp, ignore_errors=True)
        live_preview._sessions.clear()

    def _get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=5) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read()

    def _post(self, path, payload):
        req = urllib.request.Request(
            self.base + path, json.dumps(payload).encode("utf-8"),
            {"Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())

    def test_post_route_alive(self):
        """do_POST 分发生效的直接证据(本类存在的初衷)。"""
        st, d = self._post("/api/config", {})
        self.assertEqual(st, 200)
        self.assertTrue(d["ok"])

    def test_live_state_route(self):
        st, _ct, body = self._get(
            "/api/live/%E4%B8%8D%E5%AD%98%E5%9C%A8%E7%9A%84%E4%B8%BB%E6%92%AD")
        d = json.loads(body)
        self.assertEqual(st, 200)
        self.assertFalse(d["exists"])
        self.assertFalse(d["session"]["active"])
        self.assertEqual(d["session"]["state"], "absent")
        self.assertIn("m3u8", d)

    def test_live_start_route_gated_by_recording(self):
        # 路由连通性 + 业务闸门:未在录制 → 400,且绝不启动 ffmpeg
        st, d = self._post("/api/live/%E4%B8%BB%E6%92%AD/start", {})
        self.assertEqual(st, 400)
        self.assertFalse(d["ok"])
        self.assertIn("未在录制", d["error"])

    def test_danmaku_live_route(self):
        st, _ct, body = self._get(
            "/api/danmaku/live/%E4%B8%8D%E5%AD%98%E5%9C%A8%E7%9A%84%E4%B8%BB%E6%92%AD?after=0")
        d = json.loads(body)
        self.assertEqual(st, 200)
        self.assertFalse(d["active"])

    def test_live_page_served(self):
        st, ct, body = self._get("/live/%E4%B8%BB%E6%92%AD")
        self.assertEqual(st, 200)
        self.assertIn("text/html", ct)
        self.assertIn("hls.min.js".encode(), body)

    def test_vendor_served(self):
        st, ct, _body = self._get("/vendor/hls.min.js")
        self.assertEqual(st, 200)
        self.assertIn("javascript", ct)

    def test_vendor_traversal_guarded(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self._get("/vendor/..%2Findex.html")
        self.assertEqual(cm.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
