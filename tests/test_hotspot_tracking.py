#!/usr/bin/env python3
"""开播时间三源分级(_record_open)与 create_time 提取(extract_create_time)测试。

S1 create_time:检测响应透传的真实开播时间 → 优先采用;
S2 witness_mid:见证区间中点;
S3 skip:重启恢复会话/见证区间过大 且 主播已有数据 → 不记录(防污染);
新主播(无任何数据)豁免 S3:首播必记录。
"""
import os
import tempfile
import unittest
from datetime import datetime

from schedule import HotspotStore
from detect import extract_create_time
import monitor


def _ts(day, h, m):
    return datetime(2026, 8, day, h, m).timestamp()


CFG = {"schedule": {"max_witness_gap": 7200}}


class TestRecordOpenDecision(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmpdir.name, "hotspots.json")
        self.epath = os.path.join(self.tmpdir.name, "hotspot_events.json")

    def tearDown(self):
        self.tmpdir.cleanup()

    def make_store(self):
        return HotspotStore(self.path, dict(CFG["schedule"]), self.epath)

    def test_s1_create_time_wins(self):
        """响应带 create_time → 直接采用真实开播时间。"""
        store = self.make_store()
        now = _ts(20, 12, 0)
        res = {"room_id": "123", "create_time": now - 3600}
        ts = monitor._record_open(store, "A", "a1", res, None, now, CFG)
        self.assertEqual(ts, now - 3600)
        self.assertEqual(store.events["a1"][0]["src"], "create_time")

    def test_s1_rejects_stale_create_time(self):
        """create_time 超过 1 天(陈旧值)→ 降级走见证区间。"""
        store = self.make_store()
        now = _ts(20, 12, 0)
        prev = {"was_live": False, "last_room_id": None, "last_check_ts": now - 300}
        res = {"room_id": "123", "create_time": now - 2 * 86400}
        ts = monitor._record_open(store, "A", "a1", res, prev, now, CFG)
        self.assertEqual(ts, now - 150)  # 见证区间中点
        self.assertEqual(store.events["a1"][0]["src"], "witness_mid")

    def test_s2_witness_midpoint(self):
        """无 create_time,正常运行中检测到开播 → 见证区间中点。"""
        store = self.make_store()
        now = _ts(20, 12, 0)
        prev = {"was_live": False, "last_room_id": None, "last_check_ts": now - 300}
        res = {"room_id": "123"}
        ts = monitor._record_open(store, "A", "a1", res, prev, now, CFG)
        self.assertEqual(ts, now - 150)

    def test_s3_skip_on_restart_pollution(self):
        """重启时已在播(同房间号、was_live)且已有数据 → 跳过,不污染。"""
        store = self.make_store()
        now = _ts(20, 12, 0)
        store.record_open("a1", now - 86400, "seed", now)  # 制造既有数据
        prev = {"was_live": True, "last_room_id": "123", "last_check_ts": now - 600}
        res = {"room_id": "123"}  # 无 create_time
        self.assertIsNone(monitor._record_open(store, "A", "a1", res, prev, now, CFG))
        self.assertEqual(len(store.events["a1"]), 1)  # 只有 seed,无新事件

    def test_new_streamer_exempt_from_s3(self):
        """新主播(无任何数据)添加时已在播 → 仍记录首播(用户需求)。"""
        store = self.make_store()
        now = _ts(20, 12, 0)
        prev = {"was_live": True, "last_room_id": "123", "last_check_ts": now - 600}
        res = {"room_id": "123"}
        ts = monitor._record_open(store, "A", "a2", res, prev, now, CFG)
        self.assertIsNotNone(ts)
        self.assertEqual(store.events["a2"][0]["src"], "witness_mid")

    def test_s3_skip_on_large_witness_gap(self):
        """停机/熔断过久导致见证区间 > 2h 且无 create_time → 跳过(估计不可靠)。"""
        store = self.make_store()
        now = _ts(20, 12, 0)
        store.record_open("a1", now - 86400, "seed", now)
        prev = {"was_live": False, "last_room_id": None, "last_check_ts": now - 4 * 3600}
        res = {"room_id": "999"}  # 停机期间换了新房间
        self.assertIsNone(monitor._record_open(store, "A", "a1", res, prev, now, CFG))

    def test_room_changed_after_restart_records(self):
        """重启期间主播下播又开播(房间号变化)且区间可控 → 正常记录中点。"""
        store = self.make_store()
        now = _ts(20, 12, 0)
        store.record_open("a1", now - 86400, "seed", now)
        prev = {"was_live": False, "last_room_id": "old", "last_check_ts": now - 1800}
        res = {"room_id": "999"}
        ts = monitor._record_open(store, "A", "a1", res, prev, now, CFG)
        self.assertEqual(ts, now - 900)


class TestExtractCreateTime(unittest.TestCase):
    def test_escaped_json(self):
        html = r'some\"status\":2,\"ownerUserId\":1,\"create_time\":1724200000,\"x\"'
        self.assertEqual(extract_create_time(html), 1724200000)

    def test_plain_json(self):
        self.assertEqual(extract_create_time('"create_time":1724200000'), 1724200000)

    def test_camel_case_not_matched(self):
        """用户资料的 createTime(驼峰)不能被误提取。"""
        self.assertIsNone(extract_create_time('"createTime":1724200000'))

    def test_absent(self):
        self.assertIsNone(extract_create_time("no time here"))


if __name__ == "__main__":
    unittest.main()
