"""health.py 心跳判定 + webui 健康注入的单元测试。

判定口径是这个模块的全部价值所在:宁可迟报也不能误报,所以阈值测试
要覆盖「刚好在边界内/外」两种情况。
"""
import time
import unittest

import health
import webui


def _set_beat(key, ts):
    """直接写入心跳时间戳(beat() 只接受当前时间,测历史需要回溯)。"""
    with health._LOCK:
        health._BEATS[key] = ts


class HeartbeatJudgeTest(unittest.TestCase):
    def setUp(self):
        health.reset()

    def test_beat_records_timestamp(self):
        before = time.time()
        health.beat("watchdog")
        self.assertGreaterEqual(health.last_beat("watchdog"), before)

    def test_fresh_beat_is_ok(self):
        now = time.time()
        _set_beat("watchdog", now - 10)
        s = health.snapshot(now=now)
        item = self._item(s, "watchdog")
        self.assertEqual(item["status"], "ok")

    def test_age_over_grace_is_warn(self):
        now = time.time()
        grace = health.SPECS["watchdog"][3]
        _set_beat("watchdog", now - grace - 1)
        item = self._item(health.snapshot(now=now), "watchdog")
        self.assertEqual(item["status"], "warn")

    def test_age_over_double_grace_is_down(self):
        now = time.time()
        grace = health.SPECS["watchdog"][3]
        _set_beat("watchdog", now - grace * 2 - 1)
        item = self._item(health.snapshot(now=now), "watchdog")
        self.assertEqual(item["status"], "down")

    def test_never_beaten_is_idle_within_startup_grace(self):
        # 刚启动:长周期任务(6h/12h)还没跑第一轮,不能显示成故障
        now = time.time()
        item = self._item(health.snapshot(now=now), "retention")
        self.assertEqual(item["status"], "idle")

    def test_never_beaten_is_down_after_startup_grace(self):
        now = time.time() + health.STARTUP_GRACE + 10
        item = self._item(health.snapshot(now=now), "retention")
        self.assertEqual(item["status"], "down")

    def test_error_marks_warn_but_keeps_heartbeat(self):
        now = time.time()
        health.beat("archive", error="归档失败:连接超时")
        s = health.snapshot(now=now)
        item = self._item(s, "archive")
        self.assertEqual(item["status"], "warn")
        self.assertIn("连接超时", item["detail"])

    def test_next_ok_beat_clears_error(self):
        now = time.time()
        health.beat("archive", error="临时失败")
        health.beat("archive")
        item = self._item(health.snapshot(now=now), "archive")
        self.assertEqual(item["status"], "ok")

    def test_count_is_reported(self):
        now = time.time()
        health.beat("detect", count=10)
        item = self._item(health.snapshot(now=now), "detect")
        self.assertEqual(item["count"], 10)

    def test_detect_grace_follows_cold_interval(self):
        """检测间隔可配,cold_interval 拉大时宽限必须跟着变,否则会误报 down。"""
        base = health._grace_for("detect", {})
        wide = health._grace_for(
            "detect", {"schedule": {"cold_interval": 7200}})
        self.assertGreater(wide, base)
        self.assertGreater(wide, 7200)

    def _item(self, snap, key):
        for g in snap["groups"]:
            for it in g["items"]:
                if it["key"] == key:
                    return it
        self.fail("快照里没有 %s" % key)


class SnapshotShapeTest(unittest.TestCase):
    def setUp(self):
        health.reset()

    def test_all_specs_belong_to_a_declared_group(self):
        gkeys = {g[0] for g in health.GROUPS}
        for key, spec in health.SPECS.items():
            self.assertIn(spec[0], gkeys, "%s 的分组未登记" % key)

    def test_every_spec_appears_exactly_once(self):
        keys = [it["key"] for g in
                health.snapshot(now=time.time())["groups"] for it in g["items"]]
        self.assertEqual(sorted(keys), sorted(health.SPECS.keys()))

    def test_injected_items_default_to_idle(self):
        item = [it for g in health.snapshot(now=time.time())["groups"]
                for it in g["items"] if it["key"] == "nas_mount"][0]
        self.assertEqual(item["status"], "idle")

    def test_overrides_are_applied(self):
        now = time.time()
        s = health.snapshot(
            now=now,
            overrides={"nas_mount": {"status": "down", "detail": "未挂载",
                                     "last_beat": now}})
        item = [it for g in s["groups"] for it in g["items"]
                if it["key"] == "nas_mount"][0]
        self.assertEqual(item["status"], "down")
        self.assertEqual(item["detail"], "未挂载")

    def test_faults_sort_first(self):
        now = time.time()
        _set_beat("watchdog", now - 99999)          # down
        _set_beat("analysis", now - 1)              # ok
        items = [g["items"] for g in health.snapshot(now=now)["groups"]
                 if g["key"] == "tasks"][0]
        self.assertEqual(items[0]["key"], "watchdog")

    def test_flags_reflect_counts(self):
        now = time.time()
        _set_beat("watchdog", now - 99999)
        s = health.snapshot(now=now)
        self.assertTrue(s["failed"])
        self.assertFalse(s["ok"])


class OverridesTest(unittest.TestCase):
    """webui._health_overrides:进程层与外部依赖的实时判定。"""

    class _FakeState:
        def __init__(self, cfg, status=None, nas=None):
            self.config = cfg
            self._status = status or {}
            self._nas = nas or {}

        def get_config(self):
            return self.config

        def get_status(self):
            return self._status

        def get_nas_status(self):
            return self._nas

        def get_cookie_guard(self):
            return None

    def setUp(self):
        self._orig = (webui._service_status, webui._auth_status,
                      webui.find_ffmpeg)
        webui._service_status = lambda: {"managed": True, "running": True,
                                         "source": "manual", "pid": 1}
        webui._auth_status = lambda state: {"logged_in": True, "nickname": "阿测试"}
        webui.find_ffmpeg = lambda *a: "/opt/homebrew/bin/ffmpeg"

    def tearDown(self):
        (webui._service_status, webui._auth_status, webui.find_ffmpeg) = self._orig

    def _over(self, cfg=None, **kw):
        cfg = cfg if cfg is not None else {"schedule": {}}
        return webui._health_overrides(self._FakeState(cfg, **kw))

    def test_main_and_webui_are_ok(self):
        o = self._over()
        self.assertEqual(o["main"]["status"], "ok")
        self.assertEqual(o["webui"]["status"], "ok")

    def test_launchd_warn_when_not_managed_by_it(self):
        o = self._over()
        self.assertEqual(o["launchd"]["status"], "warn")
        self.assertIn("不是它启动的", o["launchd"]["detail"])

    def test_launchd_ok_when_source_is_launchd(self):
        webui._service_status = lambda: {"managed": True, "running": True,
                                         "source": "launchd", "pid": 1}
        self.assertEqual(self._over()["launchd"]["status"], "ok")

    def test_ffmpeg_idle_when_nobody_live(self):
        self.assertEqual(self._over()["ffmpeg"]["status"], "idle")

    def test_ffmpeg_ok_with_count_when_recording(self):
        # 实时录制表用 recording:true 标记在录(不是 status 字段)
        o = self._over(status={"recordings": {
            "甲": {"recording": True}, "乙": {"recording": False}}})
        self.assertEqual(o["ffmpeg"]["status"], "ok")
        self.assertEqual(o["ffmpeg"]["count"], 1)

    def test_ffmpeg_also_accepts_status_field(self):
        """兼容 status 字段写法:两种标记任一命中即算在录。"""
        o = self._over(status={"recordings": {"甲": {"status": "recording"}}})
        self.assertEqual(o["ffmpeg"]["count"], 1)

    def test_nas_down_when_configured_but_unmounted(self):
        o = self._over(cfg={"nas": {"enabled": True}},
                       nas={"mounted": False, "backoff_s": 480})
        self.assertEqual(o["nas_mount"]["status"], "down")

    def test_nas_idle_when_not_configured(self):
        self.assertEqual(self._over()["nas_mount"]["status"], "idle")

    def test_cookie_warn_when_logged_out(self):
        webui._auth_status = lambda state: {"logged_in": False}
        o = self._over()
        self.assertEqual(o["cookie"]["status"], "warn")

    def test_ffmpeg_bin_warn_when_missing(self):
        webui.find_ffmpeg = lambda *a: None
        self.assertEqual(self._over()["ffmpeg_bin"]["status"], "warn")

    def test_ffmpeg_bin_ok_with_real_path(self):
        """find_ffmpeg 是无参函数,传参会抛 TypeError 并被吞成「未找到」——防回归。"""
        o = self._over()
        self.assertEqual(o["ffmpeg_bin"]["status"], "ok")
        self.assertTrue(o["ffmpeg_bin"]["detail"])

    def test_all_injected_keys_covered(self):
        """注入项必须覆盖 SPECS 里所有 inject 组件,否则面板会出现「未接入」灰灯。"""
        o = self._over()
        for key, spec in health.SPECS.items():
            if spec[4] == "inject":
                self.assertIn(key, o, "%s 未注入" % key)


class FmtDurTest(unittest.TestCase):
    def test_units(self):
        self.assertEqual(webui._fmt_dur(30), "30 秒")
        self.assertEqual(webui._fmt_dur(120), "2 分")
        self.assertEqual(webui._fmt_dur(3600), "1 小时")
        self.assertEqual(webui._fmt_dur(3660), "1 小时 1 分")
        self.assertEqual(webui._fmt_dur(86400), "1 天")
        self.assertEqual(webui._fmt_dur(90000), "1 天 1 小时")


if __name__ == "__main__":
    unittest.main()
