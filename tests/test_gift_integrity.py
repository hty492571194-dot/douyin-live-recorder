#!/usr/bin/env python3
"""礼物通道完整性(gift_integrity)+ 零礼物结论 的单元测试(临时目录,不碰真实数据)。

覆盖 2026-10-16 两处变更:
  1. danmaku.gift_channel_coverage:存在礼物信息的时间占比(10h 场次仅 1h 有
     礼物 → ≈10%);覆盖算法、边界间隔、零事件、缺文件。
  2. monitor 收尾链路:_history_finish 把 gift_integrity 写进历史条目。
  3. monitor 礼物健康度(2026-10-29 轻通道口径):零礼物一律 quiet(不推送),
     判据只有轻通道完整性;2026-10-30 匿名可见性抽检下线后,零礼物不再有任何
     房间档案闸(没有 muted 证据、没有 detail 加注)。

设计要点:
  - 礼夹具一律合成值(主播A);覆盖算法的锚点是「1h/10h ≈ 10%(含尾部缓冲窗)」。
  - 守卫意图:零礼物结论只能由「轻通道下发了什么 vs 落盘了什么」决定 ——
    改判定前先想清楚它该输出什么(答案:quiet,只记录不推送)。
"""
import collections
import json
import os
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import danmaku  # noqa: E402
import monitor  # noqa: E402

DAY = "2026-10-15"
STEM = "主播A-20261015-200000"


def _write_jsonl(path, rows):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return path


def _gw_path(output_path):
    """giftwatch 快照路径 —— 与 monitor._gift_record 的推导保持一致
    (<基名>.giftwatch.jsonl,剥掉 .danmaku.jsonl 后缀)。"""
    p = danmaku.jsonl_for(output_path)
    suf = ".danmaku.jsonl"
    return (p[:-len(suf)] if p.endswith(suf) else p) + ".giftwatch.jsonl"


class TestGiftChannelCoverage(unittest.TestCase):
    """danmaku.gift_channel_coverage:覆盖算法与边界。"""

    def test_user_example_one_hour_of_ten(self):
        """用户给的锚点:10h 场次仅前 1h 持续有礼物 → ≈10%(含 600s 尾部缓冲窗)。"""
        path = os.path.join(self._tmp.name, ".meta", STEM + ".danmaku.jsonl")
        start = 1000.0
        rows = [{"ts": (start + i * 600) * 1000, "type": "gift", "gift": "小花花"}
                for i in range(7)]          # 0,600,...,3600s → 连续覆盖 [0,4200]
        _write_jsonl(path, rows)
        gi = danmaku.gift_channel_coverage(path, 36000.0, started_at=start)
        self.assertIsNotNone(gi)
        self.assertEqual(gi["events"], 7)
        self.assertAlmostEqual(gi["covered_sec"], 4200.0, places=1)
        self.assertAlmostEqual(gi["ratio"], 4200 / 36000, places=3)
        self.assertGreater(gi["ratio"], 0.10)
        self.assertLess(gi["ratio"], 0.13)

    def test_gap_boundary_merge_or_split(self):
        """间隔 == gap 视为连续;超过则断开。ts 用真实量级的 epoch 毫秒。"""
        start = 1_700_000_000.0
        path = os.path.join(self._tmp.name, ".meta", "s.danmaku.jsonl")
        _write_jsonl(path, [
            {"ts": start * 1000, "type": "gift"},
            {"ts": (start + 99) * 1000, "type": "gift"},   # 99s < 100s gap → 连续
        ])
        gi = danmaku.gift_channel_coverage(path, 200.0, started_at=start, gap_sec=100)
        self.assertAlmostEqual(gi["covered_sec"], 199.0, places=1)
        _write_jsonl(path, [
            {"ts": start * 1000, "type": "gift"},
            {"ts": (start + 150) * 1000, "type": "gift"},  # 150s > 100s gap → 断开
        ])
        gi = danmaku.gift_channel_coverage(path, 200.0, started_at=start, gap_sec=100)
        # [0,100] + [150,200](第二事件自身的在场窗被场次末尾裁掉一半)
        self.assertAlmostEqual(gi["covered_sec"], 150.0, places=1)
        self.assertAlmostEqual(gi["ratio"], 0.75, places=3)

    def test_tail_clipped_to_duration(self):
        """最后事件的缓冲窗不得超出场次时长。"""
        start = 1000.0
        path = os.path.join(self._tmp.name, ".meta", "s.danmaku.jsonl")
        _write_jsonl(path, [{"ts": (start + 90) * 1000, "type": "gift"}])
        gi = danmaku.gift_channel_coverage(path, 100.0, started_at=start, gap_sec=600)
        self.assertAlmostEqual(gi["covered_sec"], 10.0, places=1)
        self.assertEqual(gi["ratio"], 0.1)

    def test_zero_events_is_zero_ratio(self):
        path = os.path.join(self._tmp.name, ".meta", "s.danmaku.jsonl")
        _write_jsonl(path, [{"ts": 1000, "type": "chat", "content": "你好"}])
        gi = danmaku.gift_channel_coverage(path, 3600.0)
        self.assertEqual(gi["ratio"], 0.0)
        self.assertEqual(gi["events"], 0)

    def test_missing_file_or_bad_duration_returns_none(self):
        self.assertIsNone(
            danmaku.gift_channel_coverage("/nonexistent/x.danmaku.jsonl", 100.0))
        path = os.path.join(self._tmp.name, ".meta", "s.danmaku.jsonl")
        _write_jsonl(path, [{"ts": 0, "type": "gift"}])
        self.assertIsNone(danmaku.gift_channel_coverage(path, 0))
        self.assertIsNone(danmaku.gift_channel_coverage(None, 100.0))

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)


class TestLightGiftIntegrity(unittest.TestCase):
    """轻通道完整性对账(2026-10-29 健康度唯一判据):收到 vs 落盘 vs 字段。

    口径边界(改代码前先读):轻通道只承诺 ts/gift/diamond/count 四个字段 ——
    送礼人昵称、送礼人 uid、礼物名**本来就不下发**(实测 100%,登录态同样没有),
    所以它们永远不参与完整性判定。谁把"缺送礼人"加回判据,谁就会让每一天
    都变成不完整。
    """

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.start = 1_700_000_000.0

    def _path(self, rows):
        p = os.path.join(self._tmp.name, ".meta", "主播A-20261015-200000.danmaku.jsonl")
        _write_jsonl(p, rows)
        return p

    def _gift(self, off, gid="小心心", count=1, diamond=1):
        return {"ts": (self.start + off) * 1000, "type": "gift",
                "channel": "light", "gift": gid, "count": count, "diamond": diamond,
                "user": "", "uid": ""}

    def test_complete_when_all_received_rows_kept_with_fields(self):
        p = self._path([self._gift(60), self._gift(120, count=2, diamond=2999)])
        gi = danmaku.gift_channel_coverage(p, 600.0, started_at=self.start, recv=2)
        light = gi["light"]
        self.assertEqual(light["status"], "complete")
        self.assertTrue(light["complete"])
        self.assertEqual((light["recv"], light["events"], light["lost"]), (2, 2, 0))
        self.assertEqual(light["field_missing"], {})
        self.assertEqual(light["ratio"], 1.0)
        self.assertEqual(gi["scope"], "light", "口径必须自报 scope,防下游误用旧语义")

    def test_missing_field_makes_it_partial(self):
        row = self._gift(60)
        row.pop("diamond")                      # 单价没取到
        p = self._path([row])
        light = danmaku.gift_channel_coverage(p, 600.0, started_at=self.start,
                                              recv=1)["light"]
        self.assertEqual(light["status"], "partial")
        self.assertFalse(light["complete"])
        self.assertEqual(light["field_missing"], {"diamond": 1})
        self.assertIn("field_missing", light["reasons"])

    def test_parse_error_alone_does_not_hide_in_fields(self):
        """解析失败的事件不在 jsonl 里,不能算"字段缺失",但必须体现在 ratio 上。"""
        p = self._path([self._gift(60)])
        light = danmaku.gift_channel_coverage(p, 600.0, started_at=self.start,
                                              recv=3, parse_errors=2)["light"]
        self.assertEqual(light["status"], "partial")
        self.assertEqual(light["errors"], {"parse": 2, "write": 0})
        self.assertEqual(light["lost"], 0, "解析失败已归因,不算无法解释的缺口")
        self.assertLess(light["ratio"], 1.0, "收到 3 条只落 1 条,完整率必须被压低")
        self.assertIn("parse_error", light["reasons"])

    def test_unexplained_gap_is_event_lost(self):
        """收到 5 条、落盘 3 条、无任何失败计数 → 对不上的账 = suspect 级缺口。"""
        p = self._path([self._gift(60), self._gift(120), self._gift(180)])
        light = danmaku.gift_channel_coverage(p, 600.0, started_at=self.start,
                                              recv=5)["light"]
        self.assertEqual(light["status"], "partial")
        self.assertEqual(light["lost"], 2)
        self.assertIn("event_lost", light["reasons"])

    def test_channel_off_is_not_a_defect(self):
        """本场一条轻礼物都没有 = 服务端未下发(能力边界),不能判成不完整。"""
        p = self._path([{"ts": self.start * 1000, "type": "chat", "content": "你好"}])
        gi = danmaku.gift_channel_coverage(p, 600.0, started_at=self.start)
        self.assertEqual(gi["light"]["status"], "channel_off")
        self.assertIsNone(gi["light"]["complete"], "通道未开不能谎报「不完整」")
        self.assertEqual(gi["gift_channel"], "quiet")

    def test_no_recv_falls_back_to_field_only(self):
        p = self._path([self._gift(60)])
        light = danmaku.gift_channel_coverage(p, 600.0, started_at=self.start)["light"]
        self.assertEqual(light["status"], "field_only")
        self.assertIsNone(light["recv"])
        self.assertIsNone(light["lost"], "拿不到收到数就不该编一个缺口出来")

    def test_full_channel_rows_stay_out_of_scope(self):
        """口径外的完整通道行(light-only 之后正常为 0)单独计数,不混入轻通道。"""
        rows = [self._gift(60),
                {"ts": (self.start + 120) * 1000, "type": "gift", "channel": "full",
                 "gift": "大火箭", "count": 1, "diamond": 100}]
        light = danmaku.gift_channel_coverage(self._path(rows), 600.0,
                                              started_at=self.start, recv=1)["light"]
        self.assertEqual(light["events"], 1)
        self.assertEqual(light["full_rows"], 1)
        self.assertEqual(light["status"], "complete")

    def test_legacy_rows_without_channel_count_as_light(self):
        """2026-10-29 之前的历史行没有 channel 字段,来源只有轻通道 → 算轻通道。"""
        rows = [{"ts": (self.start + 60) * 1000, "type": "gift",
                 "gift": "礼物#16009", "count": 1, "diamond": 2999}]
        light = danmaku.gift_channel_coverage(self._path(rows), 600.0,
                                              started_at=self.start, recv=1)["light"]
        self.assertEqual(light["events"], 1)
        self.assertEqual(light["status"], "complete")


class _HistoryFixture(unittest.TestCase):
    """_history_finish 集成夹具(拦落盘,临时目录自造 jsonl)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = self.tmp.name
        self.session = os.path.join(self.base, "recordings", "主播A", DAY)
        self.out = os.path.join(self.session, STEM + "-%03d.flv")

        self.orig_save = monitor._save_history
        self.orig_history = monitor._history
        self.orig_web = monitor._web_state
        monitor._save_history = lambda: None
        monitor._web_state = None
        monitor._history = []

    def tearDown(self):
        monitor._save_history = self.orig_save
        monitor._history = self.orig_history
        monitor._web_state = self.orig_web

    def _finish(self, jsonl_rows=None, started=1000.0, ended=None):
        ended = ended if ended is not None else started + 36000.0
        if jsonl_rows is not None:
            jp = os.path.join(self.session, ".meta", STEM + ".danmaku.jsonl")
            _write_jsonl(jp, jsonl_rows)
        monitor._history_add(None, "主播A", self.out, started)
        monitor._history_finish(None, "主播A", ended_at=ended)
        return monitor._history[0]


class TestHistoryFinishGiftIntegrity(_HistoryFixture):
    """收尾时把礼物完整性写进历史条目。"""

    def test_finish_writes_gift_integrity(self):
        start = 1000.0
        rows = [{"ts": (start + i * 600) * 1000, "type": "gift", "gift": "小花花"}
                for i in range(7)]
        e = self._finish(jsonl_rows=rows, started=start)
        gi = e.get("gift_integrity")
        self.assertIsNotNone(gi, "收尾条目必须带 gift_integrity")
        self.assertEqual(gi["events"], 7)
        self.assertAlmostEqual(gi["ratio"], 4200 / 36000, places=3)

    def test_finish_without_jsonl_omits_field(self):
        e = self._finish(jsonl_rows=None)
        self.assertNotIn("gift_integrity", e)

    def test_zero_gift_rows_still_recorded_as_zero(self):
        rows = [{"ts": 1000, "type": "chat", "content": "主播好"}]
        e = self._finish(jsonl_rows=rows)
        gi = e.get("gift_integrity")
        self.assertIsNotNone(gi)
        self.assertEqual(gi["ratio"], 0.0)
        self.assertEqual(gi["events"], 0)

    def test_finish_reconciles_light_counts(self):
        """收尾必须把实时计数(收到/解析失败)带进 gift_integrity 对账。

        不带的话,历史页只能验字段齐不齐,永远发现不了"收到了却没落盘"。
        """
        start = 1000.0
        rows = [{"ts": (start + 60) * 1000, "type": "gift", "channel": "light",
                 "gift": "小心心", "count": 1, "diamond": 1}]
        dm = types.SimpleNamespace(gift_light_recv=2, gift_light_count=1,
                                   light_gift_errors=1, light_write_errors=0)
        with mock.patch.object(monitor.danmaku_mod, "get", lambda n: dm):
            e = self._finish(jsonl_rows=rows, started=start)
        light = e["gift_integrity"]["light"]
        self.assertEqual(light["recv"], 2)
        self.assertEqual(light["events"], 1)
        self.assertEqual(light["errors"], {"parse": 1, "write": 0})
        self.assertEqual(light["status"], "partial")
        self.assertIn("parse_error", light["reasons"])


class _FakeDM:
    """DanmakuRecorder 的最小替身:_gift_diagnose / _gift_record 用到的字段。"""

    def __init__(self, msg_count=600, dead=False, gift_errors=0, last_msg_ago=5.0,
                 light_recv=0, light_kept=0, light_errors=0):
        self.dead = dead
        self.retries = 0
        self.gift_errors = gift_errors
        self.gift_light_recv = light_recv
        self.gift_light_count = light_kept
        self.light_gift_errors = light_errors
        self.light_write_errors = 0
        self.light_field_miss = {}
        self.gift_full_count = 0
        self.msg_count = msg_count
        self.last_msg_ts = time.time() - last_msg_ago
        self.last_gift_ts = 0.0
        self.gift_count = 0
        self.silent_errors = 0
        self.frame_errors = 0
        self.method_counter = collections.Counter({"WebcastChatMessage": msg_count})


class TestZeroGiftAlwaysQuiet(unittest.TestCase):
    """零礼物一律 quiet,且**没有任何抑制闸**(2026-10-30 下线匿名可见性抽检后)。

    历史沿革:2026-10-12 起抽检档案能给零礼物降噪,anon_blind 房间还会额外落一条
    muted 证据。2026-10-30 实测证伪了该功能的前提(匿名侧 77/77 场收到的每一条
    轻礼物,登录侧也都收到了 —— 轻通道按身份无关下发),加上 2026-10-29 起零礼物
    本就 quiet 不推送,抑制闸已无对象,功能整体下线。
    守卫意图:不许再引回「按房间档案改结论」的闸 —— 结论只由轻通道完整性决定。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.session = os.path.join(self.tmp.name, "recordings", "主播A", DAY)
        self.out = os.path.join(self.session, STEM + "-%03d.flv")
        self.now = time.time()
        self.cfg = {"gift_watch": {"enabled": True, "silent_minutes": 45,
                                   "cooldown_minutes": 60, "busy_chat_rate": 2},
                    "danmaku": {"enabled": True}}
        self.orig = (monitor._recordings, monitor._gift_alerts, monitor.danmaku_mod)
        monitor._recordings = {"甲": {"started_at": self.now - 7200,
                                      "output_path": self.out}}
        monitor._gift_alerts = {}

    def tearDown(self):
        (monitor._recordings, monitor._gift_alerts, monitor.danmaku_mod) = self.orig

    def _scan(self, dm):
        stub = mock.MagicMock()
        stub.get = lambda name: dm
        stub.jsonl_for = danmaku.jsonl_for
        with mock.patch.object(monitor, "danmaku_mod", stub):
            return monitor._gift_scan(self.cfg)

    def _gw_lines(self):
        gw_path = _gw_path(self.out)
        if not os.path.exists(gw_path):
            return []
        with open(gw_path, encoding="utf-8") as f:
            return [json.loads(x) for x in f if x.strip()]

    def test_busy_room_zero_gift_is_quiet(self):
        """弹幕热闹却零轻礼物 → quiet(平台侧未下发,不是录制故障)。"""
        hits = self._scan(_FakeDM(msg_count=600))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["code"], "LIGHT_CHAN_OFF")
        self.assertEqual(hits[0]["level"], "quiet")
        self.assertFalse(monitor._gift_should_push(hits[0], {"notify_level": "suspect"}))

    def test_cold_room_zero_gift_is_quiet(self):
        """房间冷清 → LIGHT_QUIET,同样是 quiet、永不推送。"""
        hits = self._scan(_FakeDM(msg_count=60))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["code"], "LIGHT_QUIET")
        self.assertEqual(hits[0]["level"], "quiet")
        self.assertFalse(monitor._gift_should_push(hits[0], {"notify_level": "suspect"}))

    def test_no_muted_evidence_any_more(self):
        """不再落 muted 证据:反复巡检同一场次,记录里只有 quiet、没有 GIFT_CHAN_DOWN。"""
        for _ in range(2):
            self._scan(_FakeDM(msg_count=600))
        lines = self._gw_lines()
        self.assertEqual(len(lines), 1, "同场同结论受冷却约束,只落一条")
        self.assertEqual(lines[0]["level"], "quiet")
        self.assertNotEqual(lines[0]["code"], "GIFT_CHAN_DOWN")

    def test_fault_still_reported(self):
        """fault 级是真实线路故障,任何房间都必须照报。"""
        hits = self._scan(_FakeDM(dead=True))
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["level"], "fault")
        self.assertEqual(hits[0]["code"], "WS_DEAD")

    def test_calib_hooks_are_gone(self):
        """下线守卫:抽检相关符号不得复活(档案读取、加注、抑制表、后台循环)。"""
        for sym in ("_calib_snapshot", "_calib_adjust", "_gift_muted",
                    "gift_calib_loop", "_gift_calib_once"):
            self.assertFalse(hasattr(monitor, sym), f"monitor.{sym} 应已删除")
        self.assertNotIn("gift_calib", monitor.__dict__)
        src_path = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "monitor.py")
        with open(src_path, encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("gift_calib", src, "monitor.py 不得再引用抽检")


class TestGiftSessionStats(unittest.TestCase):
    """danmaku.gift_session_stats:每场礼物统计(份数/抖币/种类/top5)。"""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

    def _path(self, rows, name="s.danmaku.jsonl"):
        path = os.path.join(self._tmp.name, ".meta", name)
        _write_jsonl(path, rows)
        return path

    def test_counts_and_top5(self):
        """多事件聚合:份数=Σcount,抖币=Σ单价×份数,top 按份数降序。"""
        start = 1_700_000_000.0
        rows = [
            {"ts": (start + 1) * 1000, "type": "gift", "gift": "小花花", "count": 2, "diamond": 1},
            {"ts": (start + 2) * 1000, "type": "gift", "gift": "小花花", "count": 1, "diamond": 1},
            {"ts": (start + 3) * 1000, "type": "gift", "gift": "大火箭", "count": 1, "diamond": 100},
            {"ts": (start + 4) * 1000, "type": "chat", "content": "主播好"},
        ]
        gs = danmaku.gift_session_stats(self._path(rows))
        self.assertEqual(gs["msgs"], 3)
        self.assertEqual(gs["count"], 4)
        self.assertEqual(gs["diamonds"], 103)
        self.assertEqual(gs["kinds"], 2)
        self.assertEqual(gs["top"][0], {"gift": "小花花", "count": 3})
        self.assertEqual(gs["top"][1], {"gift": "大火箭", "count": 1})

    def test_zero_gift_rows(self):
        path = self._path([{"ts": 1000, "type": "chat", "content": "你好"}])
        gs = danmaku.gift_session_stats(path)
        self.assertEqual(gs["msgs"], 0)
        self.assertEqual(gs["count"], 0)
        self.assertEqual(gs["top"], [])

    def test_missing_file_returns_none(self):
        self.assertIsNone(danmaku.gift_session_stats(None))
        self.assertIsNone(danmaku.gift_session_stats("/nonexistent/x.danmaku.jsonl"))

    def test_top_capped_at_five(self):
        start = 1_700_000_000.0
        rows = [{"ts": (start + i) * 1000, "type": "gift", "gift": f"礼物{i}",
                 "count": 1, "diamond": 1} for i in range(8)]
        gs = danmaku.gift_session_stats(self._path(rows))
        self.assertEqual(gs["kinds"], 8)
        self.assertEqual(len(gs["top"]), 5)


class TestHistoryFinishGiftFields(_HistoryFixture):
    """收尾字段(2026-10-16 二次瘦身):integrity + diag 写;stats 明细不写。"""

    def test_finish_writes_gift_integrity_and_diag(self):
        start = 1000.0
        gw = os.path.join(self.session, ".meta", STEM + ".giftwatch.jsonl")
        os.makedirs(os.path.dirname(gw), exist_ok=True)
        with open(gw, "w", encoding="utf-8") as f:
            f.write(json.dumps({"level": "suspect", "code": "LIGHT_GIFT_PARSE_ERR"}) + "\n")
        rows = [{"ts": (start + 60) * 1000, "type": "gift", "gift": "小花花",
                 "count": 2, "diamond": 1}]
        e = self._finish(jsonl_rows=rows, started=start)
        gi = e.get("gift_integrity")
        self.assertIsNotNone(gi, "收尾条目必须带 gift_integrity")
        self.assertEqual(gi["events"], 1)
        gd = e.get("gift_diag")
        self.assertIsNotNone(gd, "收尾条目必须带 gift_diag")
        self.assertEqual(gd["suspect"], 1)
        self.assertEqual(gd["total"], 1)

    def test_finish_does_not_write_gift_stats(self):
        """2026-10-16 二次瘦身:具体礼物明细(份数/抖币/礼物榜)不再采集落盘。"""
        start = 1000.0
        rows = [{"ts": (start + 60) * 1000, "type": "gift", "gift": "小花花",
                 "count": 2, "diamond": 1}]
        e = self._finish(jsonl_rows=rows, started=start)
        self.assertNotIn("gift_stats", e,
                         "gift_stats 已下线 —— 收尾不得再写(用户明确不需要具体礼物情况)")

    def test_finish_without_jsonl_omits_fields(self):
        e = self._finish(jsonl_rows=None)
        self.assertNotIn("gift_integrity", e)
        self.assertNotIn("gift_diag", e)

    def test_gift_diag_counts_missing_file_returns_none(self):
        self.assertIsNone(monitor._gift_diag_counts(self.out))


class TestGiftwatchSessionFilter(unittest.TestCase):
    """webui._giftwatch_session_records:按场次取诊断记录(历史展开区数据源)。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def _make(self, stem="主播A-20261015-200000"):
        meta = os.path.join(self.tmp.name, "主播A", "2026-10-15", ".meta")
        os.makedirs(meta)
        gw = os.path.join(meta, stem + ".giftwatch.jsonl")
        with open(gw, "w", encoding="utf-8") as f:
            f.write(json.dumps({"level": "suspect", "code": "LIGHT_GIFT_PARSE_ERR",
                                "name": "主播A", "at": 1}) + "\n")
            f.write(json.dumps({"level": "fault", "code": "WS_DEAD",
                                "name": "主播A", "at": 2}) + "\n")
        out = os.path.join(self.tmp.name, "主播A", "2026-10-15", stem + "-%03d.flv")
        return out

    def test_session_records_from_allowed_root(self):
        import webui
        out = self._make()
        orig = webui._allowed_roots
        webui._allowed_roots = lambda cfg: [self.tmp.name]
        try:
            d = webui._giftwatch_session_records({}, out)
        finally:
            webui._allowed_roots = orig
        self.assertTrue(d["ok"])
        self.assertEqual(d["session"], "主播A-20261015-200000")
        self.assertEqual(len(d["records"]), 2)
        self.assertEqual({r["code"] for r in d["records"]},
                         {"LIGHT_GIFT_PARSE_ERR", "WS_DEAD"})

    def test_session_records_empty_when_missing(self):
        import webui
        orig = webui._allowed_roots
        webui._allowed_roots = lambda cfg: [self.tmp.name]
        try:
            d = webui._giftwatch_session_records({}, "/x/主播A-20261015-200000-%03d.flv")
        finally:
            webui._allowed_roots = orig
        self.assertTrue(d["ok"])
        self.assertEqual(d["records"], [])


class TestLazyGiftBackfill(unittest.TestCase):
    """webui._backfill_gift_fields:打开历史页自动补算礼物字段(无需停服)。

    背景:补算脚本 --apply 必须停服,用户重启后存量条目全部无数据,
    功能「看起来没实现」—— 惰性补算挂到 /api/history,每次补一小批。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.day = "2026-10-15"
        self.stem = "主播A-20261015-200000"
        self.session = os.path.join(self.tmp.name, "recordings", "主播A", self.day)
        self.out = os.path.join(self.session, self.stem + "-%03d.flv")

    def _make_meta(self, with_gw=True, with_jsonl=True):
        meta = os.path.join(self.session, ".meta")
        os.makedirs(meta, exist_ok=True)
        start = 1000.0
        if with_jsonl:
            _write_jsonl(os.path.join(meta, self.stem + ".danmaku.jsonl"),
                         [{"ts": (start + 60) * 1000, "type": "gift",
                           "gift": "小花花", "count": 1, "diamond": 1}])
        if with_gw:
            with open(os.path.join(meta, self.stem + ".giftwatch.jsonl"), "w",
                      encoding="utf-8") as f:
                f.write(json.dumps({"level": "suspect"}) + "\n")

    def _entry(self, **kw):
        e = {"name": "主播A", "status": "done", "duration": 3600.0,
             "started_at": 1000.0, "output_path": self.out,
             "archive": {"state": "none"}}
        e.update(kw)
        return e

    def _run(self, entries):
        import webui
        saved = webui._monitor_live
        webui._monitor_live = lambda: types.SimpleNamespace(
            _history=entries, _commit_history=lambda: None)
        try:
            return webui._backfill_gift_fields(None)
        finally:
            webui._monitor_live = saved

    def test_fills_missing_fields(self):
        self._make_meta()
        e = self._entry()
        self.assertTrue(self._run([e]))
        self.assertIsNotNone(e.get("gift_integrity"), "缺完整性 → 惰性补上")
        self.assertIsNotNone(e.get("gift_diag"), "缺诊断计数 → 惰性补上")
        self.assertNotIn("gift_stats", e, "明细统计已下线,不得补写")

    def test_skips_when_no_meta_files(self):
        e = self._entry()
        self.assertFalse(self._run([e]))
        self.assertNotIn("gift_integrity", e)
        self.assertNotIn("gift_diag", e)

    def test_skips_completed_and_recording(self):
        self._make_meta()
        # 已补齐(含 light 对账)的条目不重算;老结构(只有 ratio)会被补一次
        done = self._entry(gift_integrity={"ratio": 0.5, "light": {"status": "complete"}},
                           gift_diag={"total": 0})
        rec = self._entry(status="recording")
        self.assertFalse(self._run([done, rec]))
        self.assertNotIn("gift_integrity", rec, "录制中条目不补算")

    def test_refills_legacy_gift_integrity_without_light(self):
        """2026-10-29 结构升级:老条目只有时间占比,补算一次把轻通道对账补上。"""
        self._make_meta()
        e = self._entry(gift_integrity={"ratio": 0.5}, gift_diag={"total": 0})
        self.assertTrue(self._run([e]))
        self.assertIn("light", e["gift_integrity"])
        self.assertEqual(e["gift_integrity"]["light"]["status"], "field_only",
                         "补算拿不到实时计数 → 只能验字段,如标注未对账")

    def test_cap_limits_per_call(self):
        import webui
        self._make_meta()
        entries = [self._entry() for _ in range(5)]
        saved = (webui._monitor_live, webui._GIFT_LAZY_LOCAL_CAP)
        webui._monitor_live = lambda: types.SimpleNamespace(
            _history=entries, _commit_history=lambda: None)
        webui._GIFT_LAZY_LOCAL_CAP = 2
        try:
            self.assertTrue(webui._backfill_gift_fields(None))
        finally:
            webui._monitor_live, webui._GIFT_LAZY_LOCAL_CAP = saved
        filled = sum(1 for e in entries if e.get("gift_integrity"))
        self.assertEqual(filled, 2, "每次调用最多补 LOCAL_CAP 条,防请求阻塞")


if __name__ == "__main__":
    unittest.main()
