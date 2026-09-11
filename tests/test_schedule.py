#!/usr/bin/env python3
"""schedule.py 单元测试:两层追踪(实时层合并 + 分析层聚类)+ 会话层持久化。

覆盖:
- 实时层:merge_hours 内合并取较早、跨零点、上限淘汰、超阈值新建槽位
- 分析层:偏移>阈值且事件足够 → 立即重算;事件不足 → 回退实时层合并;
  近 3 天加权;聚类 Top-2;事件修剪
- 会话层:set_session 持久化、decay 跳过 _meta
"""
import json
import os
import tempfile
import unittest
from datetime import datetime

from schedule import HotspotStore


def _ts(day, h, m):
    """构造 2026-08 某天 hh:mm 的本地时间戳。"""
    return datetime(2026, 8, day, h, m).timestamp()


CFG = {
    "hotspot_half_width": 1800,
    "hotspot_max": 2,
    "hotspot_merge_hours": 5,
    "hotspot_decay_days": 14,
    "analysis_hour": 5,
    "analysis_cluster_min": 90,
    "analysis_min_events": 5,
    "event_retention_days": 7,
    "recency_weight": 2,
    "deviation_trigger_minutes": 60,
    "max_witness_gap": 7200,
}


class TestHotspotStore(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmpdir.name, "hotspots.json")
        self.epath = os.path.join(self.tmpdir.name, "hotspot_events.json")

    def tearDown(self):
        self.tmpdir.cleanup()

    def make_store(self, cfg=None):
        return HotspotStore(self.path, dict(cfg or CFG), self.epath)

    # --- 实时层 ---

    def test_merge_takes_earlier(self):
        """20:00 与 20:30 在 5h 合并阈内,中心取较早(1200,不再平均)。"""
        store = self.make_store()
        now = _ts(16, 12, 0)
        self.assertEqual(store.record_open("a", _ts(16, 20, 0), "witness_mid", now), "merged")
        self.assertEqual(store.record_open("a", _ts(16, 20, 30), "witness_mid", now), "merged")
        ws = store.windows("a")
        self.assertEqual(len(ws), 1)
        self.assertEqual(ws[0]["center"], 1200)
        self.assertEqual(ws[0]["hits"], 2)

    def test_cross_midnight_merge_takes_earlier(self):
        """23:50 与次日 00:10 合并,中心停在较早的 23:50(1430)。"""
        store = self.make_store()
        now = _ts(17, 0, 0)
        store.record_open("a", _ts(16, 23, 50), "witness_mid", now)   # 1430
        store.record_open("a", _ts(17, 0, 10), "witness_mid", now)    # 10
        ws = store.windows("a")
        self.assertEqual(len(ws), 1)
        self.assertEqual(ws[0]["center"], 1430)

    def test_new_slot_beyond_merge(self):
        """相差 6h(> 5h 阈值)各建独立槽位。"""
        store = self.make_store()
        now = _ts(16, 12, 0)
        store.record_open("a", _ts(16, 8, 0), "witness_mid", now)
        store.record_open("a", _ts(16, 14, 0), "witness_mid", now)
        ws = store.windows("a")
        self.assertEqual(len(ws), 2)
        self.assertEqual(sorted(w["center"] for w in ws), [480, 840])

    def test_max_windows_eviction(self):
        """3 个互不合并的槽位,保留最近命中的 2 个。"""
        store = self.make_store()
        base = _ts(16, 0, 0)
        store.record_open("a", _ts(16, 8, 0), "witness_mid", base)
        store.record_open("a", _ts(16, 14, 0), "witness_mid", base + 60)
        store.record_open("a", _ts(16, 20, 0), "witness_mid", base + 120)
        ws = store.windows("a")
        self.assertEqual(len(ws), 2)
        self.assertEqual(sorted(w["center"] for w in ws), [840, 1200])

    # --- 分析层 ---

    def test_deviation_triggers_analyze(self):
        """开播时间偏离现槽位 > 60min 且事件 ≥ 5 → 立即重算(Top-2 覆盖)。"""
        store = self.make_store()
        now = _ts(16, 20, 0)
        old = _ts(11, 14, 0)  # 5 天前,超近 3 天窗口 → 权重 1
        for _ in range(5):
            store.record_open("a", old, "create_time", now)
        self.assertEqual(len(store.windows("a")), 1)
        self.assertEqual(store.windows("a")[0]["center"], 840)

        # 新开播 20:00,偏离 360min > 60 → 触发分析(6 事件 ≥ 5)
        kind = store.record_open("a", now, "create_time", now)
        self.assertEqual(kind, "analyzed")
        ws = store.windows("a")
        self.assertEqual(sorted(w["center"] for w in ws), [840, 1200])
        self.assertEqual(len(store.events["a"]), 6)

    def test_insufficient_events_falls_back_to_realtime(self):
        """偏离 > 60min 但事件 < 5 → 分析跳过,回退实时层新建槽位(不丢)。"""
        store = self.make_store()
        now = _ts(16, 20, 0)
        store.record_open("a", _ts(16, 14, 0), "create_time", now)
        store.record_open("a", _ts(16, 14, 5), "create_time", now)
        kind = store.record_open("a", now, "create_time", now)  # 20:00,3 事件
        self.assertEqual(kind, "merged")
        ws = store.windows("a")
        self.assertEqual(sorted(w["center"] for w in ws), [840, 1200])

    def test_analyze_recency_weight(self):
        """近 3 天事件权重 ×2:近期簇强度压过更早的簇,排到第一位。"""
        store = self.make_store()
        now = _ts(16, 16, 12)
        store.events["a"] = [
            {"ts": _ts(10, 10, 0), "src": "create_time"},    # 6 天前 10:00 ×3 → 强度 3
            {"ts": _ts(10, 10, 5), "src": "create_time"},
            {"ts": _ts(10, 10, 10), "src": "create_time"},
            {"ts": _ts(16, 16, 2), "src": "create_time"},    # 今天 16:02~16:12 ×3 → 强度 6
            {"ts": _ts(16, 16, 7), "src": "create_time"},
            {"ts": _ts(16, 16, 12), "src": "create_time"},
        ]
        self.assertTrue(store.analyze("a", now))
        ws = store.windows("a")
        self.assertEqual(len(ws), 2)
        self.assertEqual(ws[0]["center"], 967)   # 近期簇加权中心
        self.assertEqual(ws[0]["strength"], 6)
        self.assertEqual(ws[1]["center"], 605)   # 10:00/10:05/10:10 均值
        self.assertEqual(ws[1]["strength"], 3)

    def test_analyze_skips_when_few_events(self):
        """事件 < 5 → analyze 返回 False,保留现槽位。"""
        store = self.make_store()
        now = _ts(16, 12, 0)
        store.events["a"] = [{"ts": now - 3600, "src": "create_time"}] * 3
        store.record_open("a", _ts(16, 14, 0), "create_time", now)  # 第 4 个事件
        before = json.dumps(store.windows("a"))
        self.assertFalse(store.analyze("a", now))
        self.assertEqual(json.dumps(store.windows("a")), before)

    def test_prune_events(self):
        """超过 event_retention_days 的事件被修剪。"""
        store = self.make_store()
        now = _ts(16, 12, 0)
        store.events["a"] = [{"ts": now - 8 * 86400, "src": "create_time"},
                             {"ts": now - 3600, "src": "create_time"}]
        self.assertTrue(store.prune_events(now))
        self.assertEqual(len(store.events["a"]), 1)

    def test_analyze_cross_midnight_cluster(self):
        """跨零点簇(23:50 与 00:10)聚为一簇,中心落在 23:5x~00:0x。"""
        store = self.make_store()
        now = _ts(16, 12, 0)
        store.events["a"] = [
            {"ts": _ts(d, 23, 50), "src": "create_time"} for d in (10, 11, 12)
        ] + [
            {"ts": _ts(d, 0, 10), "src": "create_time"} for d in (11, 12, 13)
        ]
        self.assertTrue(store.analyze("a", now))
        ws = store.windows("a")
        self.assertEqual(len(ws), 1)
        self.assertTrue(ws[0]["center"] >= 1430 or ws[0]["center"] <= 10,
                        f"跨零点簇中心异常: {ws[0]['center']}")

    # --- 会话层 ---

    def test_session_persistence(self):
        """set_session 落盘后,新实例(模拟重启)能恢复会话状态。"""
        store = self.make_store()
        now = _ts(16, 12, 0)
        store.set_session("a1", "12345", True, now)
        store2 = self.make_store()  # 重新加载同一文件
        sess = store2.get_session("a1")
        self.assertEqual(sess["last_room_id"], "12345")
        self.assertTrue(sess["was_live"])
        self.assertEqual(sess["last_check_ts"], now)

    def test_touch_session_keeps_state(self):
        """touch 只延长 last_check_ts,不动房间/在播状态。"""
        store = self.make_store()
        now = _ts(16, 12, 0)
        store.set_session("a1", "12345", True, now)
        store.touch_session("a1", now + 300)
        sess = store.get_session("a1")
        self.assertEqual(sess["last_room_id"], "12345")
        self.assertTrue(sess["was_live"])
        self.assertEqual(sess["last_check_ts"], now + 300)

    def test_decay_skips_meta(self):
        """decay 不淘汰 _meta 会话状态(仅清理 30 天未活动)。"""
        store = self.make_store()
        now = _ts(16, 12, 0)
        store.set_session("a1", "12345", True, now)
        store.record_open("a1", now, "create_time", now)
        store.data["a1"][0]["last_hit"] = now - 20 * 86400
        store.decay()
        self.assertEqual(store.windows("a1"), [])
        self.assertIsNotNone(store.get_session("a1"))

    def test_forget(self):
        """移除主播清理会话状态,但窗口/事件保留。"""
        store = self.make_store()
        now = _ts(16, 12, 0)
        store.set_session("a1", "12345", True, now)
        store.record_open("a1", now, "create_time", now)
        store.forget("a1")
        self.assertIsNone(store.get_session("a1"))
        self.assertEqual(len(store.windows("a1")), 1)
        self.assertEqual(len(store.events["a1"]), 1)


if __name__ == "__main__":
    unittest.main()
