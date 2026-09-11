#!/usr/bin/env python3
"""Cookie 使用护栏 + 矛盾状态校验节流 的单元测试。

真实账号的登录态是消耗品,这些测试守的是「不会被滥用」这条底线:
热点门挡住无谓消耗,频率门挡住瞬时突刺,配额门兜住最坏情况。
"""
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from cookie_guard import CookieGuard  # noqa: E402
import detect  # noqa: E402
import monitor  # noqa: E402


def cfg_with(**guard):
    return {"detection": {"cookie_guard": guard, "mode": "mix"}}


class TestHotspotGate(unittest.TestCase):
    """第一道闸:非热点时段不消耗 Cookie(除非是误判代价高的场景)。"""

    def test_hotspot_allows(self):
        g = CookieGuard(cfg_with())
        self.assertEqual(g.allow("a", "live", in_hotspot=True)[1], "ok")

    def test_non_hotspot_denied(self):
        g = CookieGuard(cfg_with())
        ok, why = g.allow("a", "live", in_hotspot=False)
        self.assertFalse(ok)
        self.assertEqual(why, "not_hotspot")

    def test_conflict_exempt_from_hotspot(self):
        """矛盾状态(在播却无流地址)值得花 Cookie —— 要么救回录制,要么纠正假阳性。"""
        g = CookieGuard(cfg_with())
        self.assertEqual(g.allow("a", "conflict", in_hotspot=False)[1], "ok")

    def test_error_exempt_from_hotspot(self):
        """302 自身出错时结果不可信,需要 API 兜底。"""
        g = CookieGuard(cfg_with())
        self.assertEqual(g.allow("a", "error", in_hotspot=False)[1], "ok")

    def test_hotspot_gate_can_be_disabled(self):
        g = CookieGuard(cfg_with(hotspot_only=False))
        self.assertEqual(g.allow("a", "live", in_hotspot=False)[1], "ok")

    def test_no_cookie_never_allowed(self):
        g = CookieGuard(cfg_with(hotspot_only=False))
        ok, why = g.allow("a", "conflict", in_hotspot=True, has_cookie=False)
        self.assertFalse(ok)
        self.assertEqual(why, "no_cookie")


class TestFrequencyGate(unittest.TestCase):
    """第二道闸:全局间隔挡突刺,单主播间隔挡同一房间被反复打。"""

    def test_global_min_interval(self):
        """N 个主播挤在同一分钟时,不能一次全放行。"""
        g = CookieGuard(cfg_with(min_interval_sec=60))
        self.assertEqual(g.allow("a", "conflict")[1], "ok")
        g.record("a")
        # 立刻换一个主播再问 —— 全局间隔未到,必须挡住
        ok, why = g.allow("b", "conflict")
        self.assertFalse(ok)
        self.assertEqual(why, "global_interval")

    def test_global_interval_elapses(self):
        g = CookieGuard(cfg_with(min_interval_sec=0))
        g.record("a")
        self.assertEqual(g.allow("b", "conflict")[1], "ok")

    def test_per_streamer_interval(self):
        """302 持续假阳性时,不能每轮都对同一房间打 API。"""
        g = CookieGuard(cfg_with(min_interval_sec=0, per_streamer_interval_sec=300))
        g.record("a")
        ok, why = g.allow("a", "live", in_hotspot=True)
        self.assertFalse(ok)
        self.assertEqual(why, "streamer_interval")
        # 别的主播不受该主播的节流影响
        self.assertEqual(g.allow("b", "live", in_hotspot=True)[1], "ok")

    def test_three_streamers_same_minute(self):
        """实测峰值是 3 个主播同时处于热点 —— 全局间隔应把它们摊开。"""
        g = CookieGuard(cfg_with(min_interval_sec=45, per_streamer_interval_sec=300))
        allowed = 0
        for name in ("a", "b", "c"):
            ok, _ = g.allow(name, "live", in_hotspot=True)
            if ok:
                allowed += 1
                g.record(name)
        self.assertEqual(allowed, 1, "同一时刻最多放行 1 个,其余排队")


class TestQuotaGate(unittest.TestCase):
    """第三道闸:配额兜住最坏情况,就算前两道都被绕过也有硬上限。"""

    def test_hourly_quota(self):
        g = CookieGuard(cfg_with(min_interval_sec=0, per_streamer_interval_sec=0,
                                 hourly_quota=3))
        for i in range(3):
            self.assertEqual(g.allow(f"a{i}", "live", in_hotspot=True)[1], "ok")
            g.record(f"a{i}")
        ok, why = g.allow("a9", "live", in_hotspot=True)
        self.assertFalse(ok)
        self.assertEqual(why, "hourly_quota")

    def test_daily_quota(self):
        g = CookieGuard(cfg_with(min_interval_sec=0, per_streamer_interval_sec=0,
                                 hourly_quota=100, daily_quota=5))
        for i in range(5):
            g.record(f"a{i}")
        ok, why = g.allow("zz", "live", in_hotspot=True)
        self.assertFalse(ok)
        self.assertEqual(why, "daily_quota")

    def test_hourly_window_slides(self):
        """一小时前的用量应滚出窗口,不能永久占用配额。"""
        g = CookieGuard(cfg_with(min_interval_sec=0, per_streamer_interval_sec=0,
                                 hourly_quota=2))
        g._recent = [time.time() - 7200] * 2   # 两小时前的记录
        self.assertEqual(g.allow("new", "live", in_hotspot=True)[1], "ok")

    def test_daily_counter_resets_on_new_day(self):
        g = CookieGuard(cfg_with(min_interval_sec=0, per_streamer_interval_sec=0,
                                 daily_quota=2))
        g._used_today = 2
        g._day_key = "2000-01-01"              # 伪造一个过期的日期键
        self.assertEqual(g.allow("a", "live", in_hotspot=True)[1], "ok")


class TestGuardObservability(unittest.TestCase):
    def test_snapshot_shape(self):
        g = CookieGuard(cfg_with())
        g.allow("a", "conflict")
        g.record("a")
        g.allow("b", "live", in_hotspot=False)   # 被拒,计入 by_reason
        s = g.snapshot()
        self.assertEqual(s["used_last_hour"], 1)
        self.assertEqual(s["used_today"], 1)
        self.assertEqual(s["allowed"], 1)
        self.assertGreaterEqual(s["denied"], 1)
        self.assertIn("not_hotspot", s["by_reason"])
        self.assertIn("conflict", s["hotspot_exempt"])

    def test_reconfigure_picks_up_new_rules(self):
        g = CookieGuard(cfg_with(hotspot_only=True))
        self.assertFalse(g.allow("a", "live", in_hotspot=False)[0])
        g.configure(cfg_with(hotspot_only=False))
        self.assertTrue(g.allow("a", "live", in_hotspot=False)[0])

    def test_string_values_in_config_are_coerced(self):
        """界面表单可能把数字提交成字符串,不能因此崩溃。"""
        g = CookieGuard(cfg_with(hourly_quota="7", daily_quota="9", min_interval_sec="1.5"))
        self.assertEqual(g.rules["hourly_quota"], 7)
        self.assertEqual(g.rules["daily_quota"], 9)
        self.assertEqual(g.rules["min_interval_sec"], 1.5)


class TestStreamUrlNormalization(unittest.TestCase):
    """API 返回的是分档对象,302 返回字符串 —— 两条通道的产出必须一致。

    否则录制端拿到 dict 后 ffmpeg 收到的会是 Python 对象的 str(),直接失败。
    """

    def test_string_passthrough(self):
        self.assertEqual(detect._normalize_stream_url("http://x/a.flv"), "http://x/a.flv")

    def test_empty(self):
        self.assertEqual(detect._normalize_stream_url(None), "")
        self.assertEqual(detect._normalize_stream_url({}), "")

    def test_nested_quality_dict(self):
        su = {"flv_pull_url": {"SD1": {"main": {"flv": "http://x/sd.flv"}},
                               "FULL_HD1": {"main": {"flv": "http://x/full.flv"}}}}
        self.assertEqual(detect._normalize_stream_url(su), "http://x/full.flv")

    def test_prefers_flv_over_hls(self):
        su = {"hls_pull_url": {"HD1": "http://x/h.m3u8"},
              "flv_pull_url": {"HD1": "http://x/h.flv"}}
        self.assertEqual(detect._normalize_stream_url(su), "http://x/h.flv")

    def test_flat_quality_dict(self):
        """有些房间返回的是扁平结构,没有 main 层。"""
        su = {"flv_pull_url": {"ORIGIN": "http://x/origin.flv", "HD1": "http://x/hd.flv"}}
        self.assertEqual(detect._normalize_stream_url(su), "http://x/origin.flv")

    def test_unknown_quality_key_still_works(self):
        su = {"flv_pull_url": {"SOME_NEW_TIER": "http://x/new.flv"}}
        self.assertEqual(detect._normalize_stream_url(su), "http://x/new.flv")

    def test_garbage_returns_empty(self):
        self.assertEqual(detect._normalize_stream_url({"flv_pull_url": {"HD1": {}}}), "")


class TestConflictProbeThrottle(unittest.TestCase):
    """矛盾校验的调用方是 20 秒一轮的健康巡检 —— 不节流会把日配额刷穿。"""

    def setUp(self):
        monitor._conflict_state.clear()

    def tearDown(self):
        monitor._conflict_state.clear()

    def test_first_call_allowed(self):
        due, why = monitor._conflict_due("a", cfg_with(), "room1")
        self.assertTrue(due)
        self.assertIn("首次", why)

    def test_repeat_within_interval_denied(self):
        monitor._conflict_due("a", cfg_with(conflict_probe_interval=300), "room1")
        due, why = monitor._conflict_due("a", cfg_with(conflict_probe_interval=300), "room1")
        self.assertFalse(due)
        self.assertIn("距上次校验", why)

    def test_max_tries_cap(self):
        cfg = cfg_with(conflict_probe_interval=0, conflict_probe_max=2)
        monitor._conflict_due("a", cfg, "room1")
        monitor._conflict_due("a", cfg, "room1")
        due, why = monitor._conflict_due("a", cfg, "room1")
        self.assertFalse(due)
        self.assertIn("达上限", why)

    def test_new_room_resets_counter(self):
        """换房间 = 新的一场,计数从头开始。"""
        cfg = cfg_with(conflict_probe_interval=9999, conflict_probe_max=2)
        monitor._conflict_due("a", cfg, "room1")
        due, why = monitor._conflict_due("a", cfg, "room2")
        self.assertTrue(due)
        self.assertIn("首次", why)

    def test_other_streamer_unaffected(self):
        cfg = cfg_with(conflict_probe_interval=9999)
        monitor._conflict_due("a", cfg, "room1")
        self.assertTrue(monitor._conflict_due("b", cfg, "roomB")[0])

    def test_schedule_without_loop_does_not_raise(self):
        """同步上下文(如单元测试)里调用不应抛异常。"""
        monitor._schedule_conflict_probe(None, "a", cfg_with(), "room1")


if __name__ == "__main__":
    unittest.main()
