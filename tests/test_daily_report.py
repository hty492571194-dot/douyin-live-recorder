#!/usr/bin/env python3
"""每日质检日报的守卫测试(2026-10-10 方案 A)。

覆盖:有效期分级边界 / 触发时刻计算 / 自检项组装与 force 判定 / 日报文本 /
auth 失效告警统一走 notify 通道层。
"""
import asyncio
import os
import sys
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import monitor  # noqa: E402


def _ts(h, m):
    """构造「今天 h:m」的时间戳(配合 _seconds_until_next_run 的 localtime 逻辑)。"""
    lt = time.localtime()
    return time.mktime((lt.tm_year, lt.tm_mon, lt.tm_mday, h, m, 0, 0, 0, -1))


class FixedRng:
    def __init__(self, v):
        self.v = v

    def uniform(self, a, b):
        return self.v


DR = {"expire_warn_days": 14, "expire_alert_days": 7}


class TestExpireGrade(unittest.TestCase):
    """有效期分级:14/7 两条线是日报的牙齿,漂了就失去提前量。"""

    def test_ok_zone(self):
        self.assertEqual(monitor._expire_grade(24.1, DR)[0], "ok")
        self.assertEqual(monitor._expire_grade(14.1, DR)[0], "ok")

    def test_warn_zone(self):
        self.assertEqual(monitor._expire_grade(14, DR)[0], "warn",
                         "恰好等于黄牌线必须算 warn(≤ 语义)")
        self.assertEqual(monitor._expire_grade(8, DR)[0], "warn")

    def test_alert_zone(self):
        self.assertEqual(monitor._expire_grade(7, DR)[0], "alert")
        self.assertEqual(monitor._expire_grade(3, DR)[0], "alert")
        self.assertEqual(monitor._expire_grade(0.5, DR)[0], "alert")

    def test_none_is_ok(self):
        """会话级 Cookie(无 expires)不报警。"""
        self.assertEqual(monitor._expire_grade(None, DR)[0], "ok")


class TestNextRun(unittest.TestCase):
    """触发时刻计算:日报迟发/早发/永不发都算事故。"""

    def test_same_day_future(self):
        now = _ts(19, 0)
        w = monitor._seconds_until_next_run(4, 0, now=now, rng=FixedRng(0))
        self.assertAlmostEqual(w / 3600, 9.0, delta=0.02, msg="19:00 → 次日 4:00 应为 9h")

    def test_before_hour_today(self):
        now = _ts(3, 0)
        w = monitor._seconds_until_next_run(4, 0, now=now, rng=FixedRng(0))
        self.assertAlmostEqual(w / 3600, 1.0, delta=0.02, msg="03:00 → 今天 4:00 应为 1h")

    def test_after_hour_rolls_to_tomorrow(self):
        now = _ts(4, 30)
        w = monitor._seconds_until_next_run(4, 0, now=now, rng=FixedRng(0))
        self.assertAlmostEqual(w / 3600, 23.5, delta=0.02, msg="04:30 → 明天 4:00 应为 23.5h")

    def test_jitter_symmetric(self):
        now = _ts(19, 0)
        w1 = monitor._seconds_until_next_run(4, 30, now=now, rng=FixedRng(-1800))
        w2 = monitor._seconds_until_next_run(4, 30, now=now, rng=FixedRng(1800))
        self.assertAlmostEqual(w1 / 3600, 8.5, delta=0.02)
        self.assertAlmostEqual(w2 / 3600, 9.5, delta=0.02)

    def test_never_returns_past(self):
        """抖动可能把时刻拉到 now 之前,必须 max(60) 兜底。"""
        now = _ts(4, 0) + 10          # 刚过 4 点 10 秒
        w = monitor._seconds_until_next_run(4, 0, now=now, rng=FixedRng(0))
        self.assertGreaterEqual(w, 60, "顺延后最小间隔 60s 兜底")


def _cookie(**kw):
    base = {"ok": True, "state": "ok", "reason": "", "days_left": 24.0,
            "expire_at": time.time() + 24 * 86400, "checked": False, "last_age_h": 5.0}
    base.update(kw)
    return base


class TestReportItems(unittest.TestCase):
    """自检项组装与 force 判定:force 漏判 = 异常日报被安静模式吞掉。"""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="dr-test-")
        self.cfg = {"daily_report": dict(monitor.DEFAULT_CONFIG["daily_report"]),
                    "recorder": {"output_dir": self.tmp},
                    "archive": {"mode": "external", "external_dir": ""},
                    "detection": {}, "auth": {"enabled": True}}

    def test_missing_external_dir_forces(self):
        """外接盘未插入(路径不存在)必须单独点名并 force —— 归档正停着呢。"""
        self.cfg["archive"] = {"mode": "external", "external_dir": "/Volumes/不存在的卷"}
        with mock.patch.object(monitor.health_mod, "snapshot", return_value={"groups": []}), \
             mock.patch.object(monitor.nas_mod, "queue_status", return_value={"failed": 0}), \
             mock.patch.object(monitor, "_history", []):
            rows, force = monitor._daily_report_items(_cookie(), None, self.cfg)
        self.assertTrue(force)
        row = next(r for r in rows if r[1] == "磁盘 · 归档盘")
        self.assertIn("未挂载", row[2])

    def test_healthy_report_no_force(self):
        with mock.patch.object(monitor.health_mod, "snapshot", return_value={"groups": []}), \
             mock.patch.object(monitor.nas_mod, "queue_status", return_value={"failed": 0}), \
             mock.patch.object(monitor, "_history", []):
            rows, force = monitor._daily_report_items(_cookie(), None, self.cfg)
        self.assertFalse(force, "全绿不应强制发送")
        self.assertTrue(all(i == "✅" for i, _, _ in rows),
                        f"全绿场景出现了异常图标: {rows}")

    def test_expired_cookie_forces(self):
        with mock.patch.object(monitor.health_mod, "snapshot", return_value={"groups": []}), \
             mock.patch.object(monitor.nas_mod, "queue_status", return_value={"failed": 0}), \
             mock.patch.object(monitor, "_history", []):
            rows, force = monitor._daily_report_items(
                _cookie(ok=False, state="expired", reason="登录态已过期"), None, self.cfg)
        self.assertTrue(force)
        self.assertIn("❌", [i for i, _, _ in rows])

    def test_expire_alert_day_forces(self):
        """剩 3 天 = 红牌,即使 always_send=False 也必须发。"""
        with mock.patch.object(monitor.health_mod, "snapshot", return_value={"groups": []}), \
             mock.patch.object(monitor.nas_mod, "queue_status", return_value={"failed": 0}), \
             mock.patch.object(monitor, "_history", []):
            rows, force = monitor._daily_report_items(
                _cookie(days_left=3.0), None, self.cfg)
        self.assertTrue(force, "红牌必须强制发送")
        self.assertIn("🔴", [i for i, _, _ in rows])

    def test_health_warn_forces(self):
        snap = {"groups": [{"items": [
            {"key": "giftwatch", "status": "down", "detail": "心跳超时"}]}]}
        with mock.patch.object(monitor.health_mod, "snapshot", return_value=snap), \
             mock.patch.object(monitor.nas_mod, "queue_status", return_value={"failed": 0}), \
             mock.patch.object(monitor, "_history", []):
            rows, force = monitor._daily_report_items(_cookie(), None, self.cfg)
        self.assertTrue(force)
        row = next(r for r in rows if r[1] == "健康面板")
        self.assertIn("giftwatch", row[2])

    def test_own_key_not_counted_as_bad(self):
        """日报协程自己的 warn 不能算进体检结果 —— 否则自己告自己,死循环。"""
        snap = {"groups": [{"items": [
            {"key": "dailyreport", "status": "warn", "detail": "心跳延迟"}]}]}
        with mock.patch.object(monitor.health_mod, "snapshot", return_value=snap), \
             mock.patch.object(monitor.nas_mod, "queue_status", return_value={"failed": 0}), \
             mock.patch.object(monitor, "_history", []):
            rows, force = monitor._daily_report_items(_cookie(), None, self.cfg)
        self.assertFalse(force, "排除自身后应全绿")
        self.assertIn("✅", [i for i, _, _ in rows])

    def test_archive_failed_forces(self):
        hist = [{"archive": {"state": "failed"}}, {"archive": {"state": "archived"}}]
        with mock.patch.object(monitor.health_mod, "snapshot", return_value={"groups": []}), \
             mock.patch.object(monitor.nas_mod, "queue_status", return_value={"failed": 0}), \
             mock.patch.object(monitor, "_history", hist):
            rows, force = monitor._daily_report_items(_cookie(), None, self.cfg)
        self.assertTrue(force)
        row = next(r for r in rows if r[1] == "归档")
        self.assertIn("历史失败 1", row[2])

    def test_disk_low_forces(self):
        fake = mock.Mock()
        fake.free = 5 * 2**30
        with mock.patch.object(monitor.health_mod, "snapshot", return_value={"groups": []}), \
             mock.patch.object(monitor.nas_mod, "queue_status", return_value={"failed": 0}), \
             mock.patch.object(monitor, "_history", []), \
             mock.patch.object(monitor.shutil, "disk_usage", return_value=fake):
            rows, force = monitor._daily_report_items(_cookie(), None, self.cfg)
        self.assertTrue(force)
        self.assertIn("🔴", [i for i, _, _ in rows])


class TestReportText(unittest.TestCase):
    def test_normal_text(self):
        txt = monitor._daily_report_text(
            [("✅", "登录态", "校验通过")], now=_ts(4, 7))
        self.assertIn("每日质检日报", txt)
        self.assertIn("全部正常", txt)

    def test_bad_text_counts(self):
        txt = monitor._daily_report_text(
            [("❌", "登录态", "失效"), ("✅", "归档", "无失败")], now=_ts(4, 7))
        self.assertIn("1 项需关注", txt)
        self.assertIn("失效", txt)


class TestAuthNotifyUnified(unittest.TestCase):
    """登录态失效告警必须走 notify 通道层 —— 飞书旧通道已废,谁写回去谁报红。"""

    def test_notify_uses_push_by_config(self):
        import auth
        calls = []

        async def fake_push(gw, title, body="", local=None):
            calls.append({"gw": gw, "title": title, "body": body})
            return {"ok": True, "channels": ["wecom-cli"], "error": ""}

        cfg = {"gift_watch": {"notify_channel": "auto", "webhook_url": ""}}
        with mock.patch.object(auth, "_notify", wraps=auth._notify), \
             mock.patch("notify.push_by_config", new=fake_push):
            ok = asyncio.run(auth._notify(cfg, "抖音登录态失效", "expired: 请重新扫码"))
        self.assertTrue(ok)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["gw"], cfg["gift_watch"],
                         "告警通道必须读 gift_watch 全局告警配置")
        self.assertIn("登录态失效", calls[0]["title"])

    def test_notify_never_raises(self):
        import auth

        async def boom(*a, **kw):
            raise RuntimeError("通道炸了")

        with mock.patch("notify.push_by_config", new=boom):
            ok = asyncio.run(auth._notify({}, "t", "d"))
        self.assertFalse(ok, "推送失败只能返回 False,绝不抛异常")


if __name__ == "__main__":
    unittest.main()
